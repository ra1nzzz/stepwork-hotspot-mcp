"""热点宝（douhot.douyin.com）采集器 —— 需要登录态，走 CDP 复用真实浏览器。

三个前置判断，写死在注释里以免后来者走回头路：

1. **没有公开 API**。热点宝的数据全在登录后 XHR 里，且请求带 ``a_bogus``
   / ``X-Bogus`` 签名。自己签不现实，也不该签（那是绕风控）。
2. **页面是微前端 SPA**（``@byted-douhot/micro``），``curl`` 只能拿到 9KB 的
   壳，必须真渲染。
3. **所以走 CDP**：连到用户**自己已经登录**的浏览器（``--remote-debugging-port``），
   复用他的登录态。我们只 goto + 读响应体/可见文本，**不碰密码、不存 cookie、
   不复制 profile**；``browser.close()`` 只是断 CDP 连接，不会关用户浏览器。

为什么 playwright 不在 ``dependencies`` 里：本 Server 的硬约束是「零运行时
依赖」（能少一个依赖就少一个「装不上」的理由）。故 CDP driver **延迟导入**，
没装时抛 :class:`SourceError` 指名安装命令 —— 不静默返回空列表。

为什么解析做成「启发式」而不是硬编码选择器：榜单页面结构与接口字段会变，
硬编码等于下个版本就烂。这里先拦 JSON 响应（准），拦不到再退到可见文本
（糙但不会全空），并在 ``meta.parse`` 里如实标注走了哪条路。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from .models import HotspotItem, SourceError

#: 热点宝站点根。榜单路径见 :data:`DEFAULT_BOARD`。
DOUHOT_ORIGIN = "https://douhot.douyin.com"

#: 默认 CDP 端点。启动浏览器时加 ``--remote-debugging-port=9222`` 即可。
DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"

#: 默认榜单（热点榜）。board 可传路径片段，如 ``"low_fans"``。
DEFAULT_BOARD = "hotspot"

#: 未装 playwright 时的安装提示（会被直接显示给用户，写全）。
PLAYWRIGHT_HINT = (
    "热点宝需要浏览器自动化：pip install 'stepwork-hotspot-mcp[browser]' "
    "&& playwright install chromium"
)

# 候选键名。**宁可多列也别漏**：接口字段名在 word/hot_word/query 之间来回换，
# 漏一个就是「页面明明有数据却一条都解析不出来」。
_TITLE_KEYS = (
    "title",
    "word",
    "hot_word",
    "hotWord",
    "hotword",
    "keyword",
    "name",
    "query",
    "sentence",
    "text",
    "desc",
    "description",
)
_VALUE_KEYS = (
    "hot_value",
    "hotValue",
    "hotvalue",
    "heat",
    "heat_value",
    "score",
    "index",
    "value",
    "num",
    "count",
    "view_count",
    "vv",
    "play_count",
    "popularity",
)
_URL_KEYS = ("url", "link", "share_url", "shareUrl", "schema", "aweme_url", "web_url")
_TIME_KEYS = (
    "publish_time",
    "publishTime",
    "create_time",
    "createTime",
    "start_time",
    "startTime",
    "time",
    "date",
    "datetime",
)
# 容器键：接口喜欢把列表裹在 data / list / result 里
_CONTAINER_KEYS = (
    "data",
    "list",
    "items",
    "result",
    "results",
    "records",
    "word_list",
    "wordList",
    "hot_list",
    "hotList",
    "board_list",
    "rank_list",
    "aweme_list",
    "card_list",
)

# 页面可见文本里的噪声行（导航/权益/提示），出现即跳过
_NOISE_RE = re.compile(
    r"^(首页|热点宝|登录|注册|扫码|下载|开通|会员|首页$|精选|榜单|我的|帮助|反馈"
    r"|上一页|下一页|更多|查看|详情|立即|免费|试用|升级|全部|筛选|搜索|时间|排名"
    r"|热度|趋势|操作|序号|标题|关键词|视频|图文|直播|评论|点赞|分享|收藏)$"
)

# 文本兜底里的热度值：12.3w / 4,567 / 1.2亿
_HEAT_RE = re.compile(r"^\s*([\d.,]+\s*[wW万kK千亿]?)\s*$")


class DouhotDriver(Protocol):
    """热点宝的数据来源抽象。

    抽出来是为了**可测**：真跑浏览器既慢又依赖登录态，单测用 fake driver
    喂快照即可覆盖解析逻辑 —— 这也是本项目其它源（HTML/JSON）的一贯做法。
    """

    def snapshot(self, board: str | None = None) -> DouhotSnapshot:
        """抓一个榜单的快照（不解析）。"""
        ...

    def list_boards(self) -> list[dict[str, str]]:
        """列出可抓的榜单（``[{"id", "title", "url"}]``）。"""
        ...


@dataclass(frozen=True)
class DouhotSnapshot:
    """一次抓取的原始结果。**未解析**，保持可审计。"""

    board: str
    #: 拦截到的、看起来像榜单的 JSON 响应体（可能多层嵌套）
    payloads: list[Any] = field(default_factory=list)
    #: 兜底用的页面可见文本（仅当 payloads 为空时才填）
    text: str = ""


def _import_playwright() -> Any:
    """延迟导入 playwright。

    顶层 import 会让「没装 browser 额外依赖」变成**整个 MCP Server 起不来**，
    而不是「这一个源不可用」。这是这里唯一必须延迟导入的理由。
    """
    try:
        from playwright import sync_api  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - 取决于安装
        raise SourceError(
            f"playwright 未安装（热点宝源不可用，其它源不受影响）。{PLAYWRIGHT_HINT}"
        ) from e
    return sync_api


class CdpDouhotDriver:
    """通过 CDP 复用用户已登录浏览器的 driver（只读）。"""

    def __init__(
        self,
        endpoint: str = DEFAULT_CDP_ENDPOINT,
        *,
        settle_ms: int = 3000,
        timeout_ms: int = 30_000,
    ) -> None:
        """
        Args:
            endpoint: CDP 地址（``http://127.0.0.1:9222``）。
            settle_ms: 导航后等待 SPA 发完 XHR 的时间。太短会抓空。
            timeout_ms: 单次导航超时。
        """
        self._endpoint = endpoint
        self._settle_ms = settle_ms
        self._timeout_ms = timeout_ms

    def snapshot(self, board: str | None = None) -> DouhotSnapshot:
        sync_api = _import_playwright()
        with sync_api.sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self._endpoint)
            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()
                payloads: list[Any] = []
                page.on("response", lambda r: _collect_json(r, payloads))
                page.goto(
                    _board_url(board),
                    wait_until="domcontentloaded",
                    timeout=self._timeout_ms,
                )
                page.wait_for_timeout(self._settle_ms)
                text = ""
                if not payloads:
                    # 兜底：拿不到 JSON 至少拿可见文本，别让整源静默空转
                    text = page.inner_text("body")
                return DouhotSnapshot(board=board or DEFAULT_BOARD, payloads=payloads, text=text)
            finally:
                # connect_over_cdp 的 close() = 断开连接，不关用户浏览器
                browser.close()

    def list_boards(self) -> list[dict[str, str]]:
        sync_api = _import_playwright()
        with sync_api.sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(self._endpoint)
            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()
                if "douhot.douyin.com" not in (page.url or ""):
                    page.goto(DOUHOT_ORIGIN, wait_until="domcontentloaded")
                    page.wait_for_timeout(self._settle_ms)
                links = page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => ({href: e.getAttribute('href') || '', "
                    "text: (e.innerText || '').trim()}))",
                )
            finally:
                browser.close()
        return _clean_links(links)


def _board_url(board: str | None) -> str:
    if not board or board == DEFAULT_BOARD:
        return f"{DOUHOT_ORIGIN}/hotspot"
    if board.startswith("http"):
        return board
    return f"{DOUHOT_ORIGIN}/{board.lstrip('/')}"


def _clean_links(links: Any) -> list[dict[str, str]]:
    """把页面所有 <a> 收敛成「站内榜单链接」列表。"""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in links or []:
        if not isinstance(link, dict):
            continue
        href = str(link.get("href") or "")
        title = _one_line(str(link.get("text") or ""))
        if not title or len(title) > 30:
            continue
        if href.startswith("/"):
            href = DOUHOT_ORIGIN + href
        if "douhot.douyin.com" not in href or href in seen:
            continue
        seen.add(href)
        out.append(
            {
                "id": href.rstrip("/").rsplit("/", 1)[-1] or "home",
                "title": title,
                "url": href,
            }
        )
    return out


def _collect_json(response: Any, sink: list[Any]) -> None:
    """响应钩子：只收「看起来是榜单」的 JSON。"""
    try:
        headers = response.headers or {}
        if "json" not in str(headers.get("content-type", "")).lower():
            return
        body = response.json()
    except Exception:  # noqa: BLE001 - 单个响应读不出来就跳过，不影响其它
        return
    if _find_item_lists(body):
        sink.append(body)


def _pick(node: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in node and node[key] not in (None, "", [], {}):
            return node[key]
    return None


def _first_list(node: Any) -> list[Any] | None:
    if isinstance(node, list) and node:
        return node
    if isinstance(node, dict):
        for key in _CONTAINER_KEYS:
            if key in node:
                found = _first_list(node[key])
                if found:
                    return found
    return None


def _looks_like_item(node: Any) -> bool:
    return isinstance(node, dict) and _pick(node, _TITLE_KEYS) is not None


def _find_item_lists(node: Any, depth: int = 0) -> list[list[dict[str, Any]]]:
    """递归找出所有「元素是带标题的 dict」的列表。

    广度不限深度（接口嵌套能到 5 层），但**不做**任何字段假设 —— 只看元素
    里有没有标题键。这样字段改名不会导致全盘失效。
    """
    if depth > 8:
        return []
    found: list[list[dict[str, Any]]] = []
    if isinstance(node, list):
        sample = node[:5]
        # 用「多数像条目」而不是「全部像条目」：真实榜单里常夹一两个
        # 广告卡/占位卡，要求 all() 会让整页解析不出一条。
        if sample and sum(1 for x in sample if _looks_like_item(x)) >= 0.6 * len(sample):
            found.append([x for x in node if _looks_like_item(x)])
        for child in node[:20]:
            found.extend(_find_item_lists(child, depth + 1))
    elif isinstance(node, dict):
        for value in list(node.values())[:30]:
            found.extend(_find_item_lists(value, depth + 1))
    return found


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    m = re.match(r"^([\d.]+)\s*([wW万kK千亿]?)$", text)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2)
    if unit in ("w", "W", "万"):
        return num * 10_000
    if unit in ("k", "K", "千"):
        return num * 1_000
    if unit == "亿":
        return num * 100_000_000
    return num


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _to_iso(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        # 10 位秒 / 13 位毫秒
        num = int(value)
        if num > 10_000_000_000:
            num //= 1000
        if num < 1_000_000_000:
            return None
        # timezone.utc 而非 datetime.UTC：后者是 3.11+，本包要求 >=3.10
        return datetime.fromtimestamp(num, timezone.utc).isoformat()
    text = _one_line(str(value))
    return text or None


def _item_from_node(node: dict[str, Any], board: str) -> HotspotItem | None:
    title = _pick(node, _TITLE_KEYS)
    title = _one_line(str(title)) if title is not None else ""
    if not title or len(title) > 200:
        return None
    summary = _one_line(str(_pick(node, ("summary", "abstract", "intro", "content")) or ""))
    return HotspotItem(
        source="douhot",
        title=title,
        url=_one_line(str(_pick(node, _URL_KEYS) or "")),
        summary=summary,
        published_at=_to_iso(_pick(node, _TIME_KEYS)),
        score=_to_float(_pick(node, _VALUE_KEYS)),
        meta={"board": board, "parse": "json"},
    )


def parse_payloads(payloads: list[Any], board: str) -> list[HotspotItem]:
    """把拦截到的 JSON 响应解析成条目（去重、保序）。"""
    items: list[HotspotItem] = []
    seen: set[str] = set()
    for payload in payloads:
        for group in _find_item_lists(payload):
            for node in group:
                item = _item_from_node(node, board)
                if item is None or item.id in seen:
                    continue
                seen.add(item.id)
                items.append(item)
    return items


def parse_text(text: str, board: str) -> list[HotspotItem]:
    """可见文本兜底解析。

    榜单页在数据没走 JSON 时，body 文本大致是「名次 / 标题 / 热度」按行排。
    这里按行扫：跳过噪声行与纯数字，遇到热度行就归入上一条。**故意保守** ——
    宁可少收也别把导航文案当选题。
    """
    items: list[HotspotItem] = []
    seen: set[str] = set()
    pending: str | None = None
    for raw in (text or "").splitlines():
        line = _one_line(raw)
        if not line:
            continue
        if _NOISE_RE.match(line) or _HEAT_RE.match(line) or line.isdigit():
            # 热度行：归给上一条待定标题
            if pending and _HEAT_RE.match(line):
                heat = _to_float(_HEAT_RE.match(line).group(1))  # type: ignore[union-attr]
                item = HotspotItem(
                    source="douhot",
                    title=pending,
                    summary="",
                    score=heat,
                    meta={"board": board, "parse": "text-fallback"},
                )
                if item.id not in seen:
                    seen.add(item.id)
                    items.append(item)
                pending = None
            continue
        if len(line) < 2 or len(line) > 100:
            continue
        if pending:
            # 上一行没等到热度值也要收：没有热度总比丢掉选题强
            item = HotspotItem(
                source="douhot",
                title=pending,
                summary="",
                score=None,
                meta={"board": board, "parse": "text-fallback"},
            )
            if item.id not in seen:
                seen.add(item.id)
                items.append(item)
        pending = line
    if pending:
        items.append(
            HotspotItem(
                source="douhot",
                title=pending,
                summary="",
                score=None,
                meta={"board": board, "parse": "text-fallback"},
            )
        )
    return items


def parse_snapshot(snapshot: DouhotSnapshot) -> list[HotspotItem]:
    """JSON 优先，文本兜底。"""
    items = parse_payloads(snapshot.payloads, snapshot.board)
    if items:
        return items
    return parse_text(snapshot.text, snapshot.board)


def _default_driver() -> DouhotDriver:
    import os  # noqa: PLC0415

    return CdpDouhotDriver(os.environ.get("DOUHOT_CDP_ENDPOINT", DEFAULT_CDP_ENDPOINT))


def fetch_douhot(
    limit: int = 30,
    *,
    driver: DouhotDriver | None = None,
    board: str | None = None,
    source_id: str = "douhot",
) -> list[HotspotItem]:
    """抓热点宝一个榜单。

    Args:
        limit: 最多返回条数。
        driver: 注入的 driver（测试用 fake；生产走 CDP）。
        board: 榜单 id 或完整 URL；``None`` = 热点榜。
        source_id: 写进条目的 ``source``。**必须与 :data:`SOURCES` 的 key 一致** ——
            下游按 ``source`` 过滤与分组做源内分位，取一个自成一套的名字
            （比如两个榜都写 ``douhot``）会让 ``sources=["douhot_low_fans"]``
            静默返回空。

    Raises:
        SourceError: playwright 未装 / CDP 连不上 / 页面需要登录。
    """
    active = driver or _default_driver()
    try:
        snapshot = active.snapshot(board)
    except SourceError:
        raise
    except Exception as e:  # noqa: BLE001 - CDP 各类异常统一转成可读错误
        raise SourceError(
            f"热点宝抓取失败（{type(e).__name__}: {e}）。"
            f"确认浏览器已用 --remote-debugging-port 启动并登录 douhot.douyin.com；"
            f"端点可用 DOUHOT_CDP_ENDPOINT 覆盖（默认 {DEFAULT_CDP_ENDPOINT}）"
        ) from e
    items = [
        replace(it, source=source_id) for it in parse_snapshot(snapshot)
    ]
    if not items:
        raise SourceError(
            "热点宝返回空：可能是未登录（页面停在登录页）或榜单结构已变。"
            "请在被连接的浏览器里打开 douhot.douyin.com 确认已登录。"
        )
    return items[:limit]


def douhot_fetcher(
    board: str | None = None, *, source_id: str = "douhot"
) -> Callable[..., list[HotspotItem]]:
    """按 board 生成注册表用的 fetch 函数（与 ``_newsnow_fetcher`` 同构）。

    ``source_id`` 默认与注册表 key 同名；注册非默认榜时务必显式传，
    否则条目会张冠李戴（两个榜的条目挂同一个 source）。
    """

    def _fetch(limit: int = 30, **_: Any) -> list[HotspotItem]:
        return fetch_douhot(limit, board=board, source_id=source_id)

    _fetch.__name__ = f"fetch_douhot_{board or DEFAULT_BOARD}"
    # 把 source_id 挂在函数上：注册表测试据此断言「每个源的 fetch 声明的
    # source 名必须等于它在 SOURCES 里的 key」—— 闭包本身看不见这个值，
    # 不挂出来就只能靠人肉记得同步改两处。
    _fetch.source_id = source_id
    return _fetch


def iter_boards(driver: DouhotDriver | None = None) -> Iterator[dict[str, str]]:
    """列出可抓榜单（供 GUI/CLI 展示「接上热点宝后可以选哪些榜」）。"""
    active = driver or _default_driver()
    yield from active.list_boards()
