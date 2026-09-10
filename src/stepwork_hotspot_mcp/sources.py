"""热点连接器。

选择标准是**能否在这台机器上真的拿到数据**，不是「文档上写得好」。

2026-09-09 首次实测：

============  ==============  ==========================================
源            结果            备注
============  ==============  ==========================================
HF daily      ✅ 200 JSON     免密钥，AI 论文，英文
GitHub Trend  ✅ 200 HTML     免密钥；抓 HTML，结构会变（故测试用固定件）
少数派 RSS    ✅ 200 XML      免密钥，中文，但偏「效率工具/数码」
arXiv API    ❌ 连不通        http 301 → https 后仍 000（本机网络）
RSSHub       ❌ 403           公共实例有反爬
36氪 /feed   ❌ 返回 HTML     不是真 RSS
InfoQ /feed  ❌ 451
微博热搜第三方 ❌ 连不通       域名不可达
============  ==============  ==========================================

**2026-09-10 翻案**：上面那张表里的 ❌ 大部分是**我的探测方法错了**，不是源
没了 —— arXiv 与 InfoQ 的根本原因分别是「http 不跟 301」和「默认客户端 UA
触发 WAF」，换成 https + 浏览器 UA 后都通；微博热搜是我拿一个已死的第三方
镜像当代理。同日新增：抖音热榜 / 头条热榜（均为**官方接口、免登录免密钥**）
+ NewsNow 五个中文聚合榜。**中文热点拿得到**，「只能追技术圈」的结论作废。

当前 13 源（``SOURCES``）：11 个免登录 + 2 个热点宝（``requires_login=True``，
走 CDP 复用用户已登录浏览器，见 :mod:`.douhot`）。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

from .douhot import douhot_fetcher
from .models import HotspotItem, SourceError

#: 站点会按 UA 挡「非浏览器客户端」——实测 InfoQ 的**公开 RSS** 对默认
#:客户端 UA 直接回 451，换浏览器 UA 就是 200。这里取的是公开 feed，
#:不涉及登录后内容，UA 只是让对方按正常浏览器对待。
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 "
    "stepwork-hotspot-mcp/0.1 (+https://github.com/ra1nzzz/stepwork-hotspot-mcp)"
)

#: 默认 RSS 源（中文科技/效率）。用 ``HOTSPOT_RSS_FEEDS`` 覆盖（逗号分隔）
DEFAULT_RSS_FEEDS = [
    "https://sspai.com/feed",
    "https://www.infoq.cn/feed",
]

_HF_DAILY = "https://huggingface.co/api/daily_papers"
_GH_TRENDING = "https://github.com/trending"
_DOUYIN_HOT = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word"
_ARXIV_API = "https://export.arxiv.org/api/query"
_TOUTIAO_HOT = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
#: NewsNow 是**开源**聚合（可自部署）；这里用的是社区公共实例，
#: 一个端点覆盖十来个中文/英文榜单，是本项目性价比最高的一个源。
_NEWNOW = "https://newsnow.busiyi.world/api/s"
#: 默认榜单：微博 / 知乎 / 抖音 / 头条 / 百度（中文社会热点为主）
DEFAULT_NEWNOW_BOARDS = ("weibo", "zhihu", "douyin", "toutiao", "baidu")


def http_text(url: str, timeout: float = 20.0) -> str:
    """GET 文本；非 2xx / 超时统一成 :class:`SourceError`（带状态码）。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - 固定 https 源
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return str(raw.decode(charset, errors="replace"))
    except urllib.error.HTTPError as e:
        raise SourceError(f"HTTP {e.code} for {url}") from None
    except Exception as e:  # noqa: BLE001 - 超时/DNS/SSL 都要变成可读错误
        raise SourceError(f"{type(e).__name__} for {url}: {e}") from None


def strip_html(text: str) -> str:
    """去标签 + 实体还原 + 压空白（RSS 描述里常整段是 HTML）。"""
    no_tags = re.sub(r"<[^>]+>", " ", text or "")
    unescaped = (
        no_tags.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return re.sub(r"\s+", " ", unescaped).strip()


# ---------------------------------------------------------------------------
# 连接器


def fetch_huggingface_daily(limit: int = 20) -> list[HotspotItem]:
    """HF Daily Papers（AI 论文，按社区热度排序）。"""
    data = json.loads(http_text(f"{_HF_DAILY}?limit={max(1, limit)}"))
    items: list[HotspotItem] = []
    for entry in data:
        paper = entry.get("paper") or {}
        title = (entry.get("title") or paper.get("title") or "").strip()
        if not title:
            continue
        summary = strip_html(entry.get("summary") or paper.get("summary") or "")
        arxiv_id = str(paper.get("id") or "")
        items.append(
            HotspotItem(
                source="huggingface_daily",
                title=title,
                url=f"https://huggingface.co/papers/{arxiv_id}" if arxiv_id else "",
                summary=summary[:600],
                # **必须**用 submittedOnDailyAt（上今日榜的时刻），不能用论文
                # 的 publishedAt：后者常是几天前，按「最近 48 小时」过滤会把
                # 整个源过滤光（实测：论文 09-05 发布，09-09 才上榜）
                published_at=paper.get("submittedOnDailyAt") or paper.get("publishedAt"),
                score=float(paper.get("upvotes") or 0) or None,
                meta={
                    "arxivId": arxiv_id,
                    "authors": paper.get("authors") or [],
                    "paperPublishedAt": paper.get("publishedAt"),
                },
            )
        )
    return items


def _parse_gh_trending(html: str, limit: int) -> list[HotspotItem]:
    """抓 GitHub Trending 的 ``<article>`` 列表。

    GitHub 没有官方 trending API，只能解析 HTML —— 所以**类名必须写得宽容**
    （实测描述块的类名带 ``tmp-`` 前缀，写死就取不到）。解析失败返回空列表
    而不是抛错：源结构变了应当表现为「这个源今天没数据」，让上层把它列进
    ``errors``，而不是拖垮整个 ``discover_hotspots``。
    """
    repos = re.findall(r'<h2 class="h3 lh-condensed">\s*<a [^>]*href="/([^"]+)"', html)
    descs = re.findall(
        r'<p class="col-9 color-fg-muted my-1[^"]*">\s*(.*?)\s*</p>', html, re.S
    )
    stars = re.findall(r"([\d,]+)\s*stars today", html)
    langs = re.findall(r'itemprop="programmingLanguage">([^<]+)<', html)

    items: list[HotspotItem] = []
    for i, repo in enumerate(repos[:limit]):
        desc = strip_html(descs[i]) if i < len(descs) else ""
        star_txt = stars[i].replace(",", "") if i < len(stars) else ""
        items.append(
            HotspotItem(
                source="github_trending",
                title=repo,
                url=f"https://github.com/{repo}",
                summary=desc[:400],
                # trending 页面不给出条目时间：按「今日榜」当成现在
                published_at=datetime.now(timezone.utc).isoformat(),
                score=float(star_txt) if star_txt.isdigit() else None,
                meta={"language": langs[i] if i < len(langs) else None, "rank": i + 1},
            )
        )
    return items


def fetch_github_trending(
    limit: int = 25, since: str = "daily", language: str | None = None
) -> list[HotspotItem]:
    """GitHub Trending（开源项目，HTML 抓取）。"""
    url = f"{_GH_TRENDING}/{quote(language)}" if language else _GH_TRENDING
    html = http_text(f"{url}?since={since}", timeout=25.0)
    return _parse_gh_trending(html, limit)


def _rss_feeds(feeds: list[str] | None = None) -> list[str]:
    if feeds:
        return feeds
    env = os.environ.get("HOTSPOT_RSS_FEEDS", "").strip()
    return [f.strip() for f in env.split(",") if f.strip()] or DEFAULT_RSS_FEEDS


def _parse_rss(xml_text: str, source_id: str) -> list[HotspotItem]:
    root = ET.fromstring(xml_text)
    items: list[HotspotItem] = []
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        if not title:
            continue
        pub = (node.findtext("pubDate") or "").strip()
        published_at: str | None = None
        if pub:
            try:
                published_at = parsedate_to_datetime(pub).astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError):
                published_at = None
        items.append(
            HotspotItem(
                source=source_id,
                title=title,
                url=(node.findtext("link") or "").strip(),
                summary=strip_html(node.findtext("description") or "")[:600],
                published_at=published_at,
                meta={"feedTitle": root.findtext("channel/title") or ""},
            )
        )
    return items


def fetch_rss(limit: int = 20, feeds: list[str] | None = None) -> list[HotspotItem]:
    """通用 RSS/Atom（默认少数派）。中文源，偏效率工具与数码。"""
    items: list[HotspotItem] = []
    errors: list[str] = []
    for feed in _rss_feeds(feeds):
        try:
            items.extend(_parse_rss(http_text(feed), "rss"))
        except (SourceError, ET.ParseError) as e:
            # 单个 feed 挂了不影响其它 feed：降级要看得见，所以要收集
            errors.append(f"{feed}: {e}")
    if not items and errors:
        raise SourceError("; ".join(errors))
    return items[:limit]


def _cn_time_to_iso(value: str) -> str | None:
    """``"2026-09-10 18:57:21"``（**北京时间**，无时区标记）→ ISO 8601。

    不加 ``+08:00`` 会被当成 UTC，时间窗过滤因此整体偏 8 小时 ——
    按 48 小时窗能差掉三分之一的条目。
    """
    try:
        naive = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return None
    return naive.replace(tzinfo=timezone(timedelta(hours=8))).isoformat()


def fetch_douyin_hot(limit: int = 50) -> list[HotspotItem]:
    """抖音热榜（官方 web 接口，免登录免密钥）。

    返回 50 条中文热榜词 + 热度值。**这是本项目里唯一一个「中文社会热点」
    且零门槛的源** —— 也是 STEPWORK 选题最贴近的一条。
    """
    data = json.loads(http_text(_DOUYIN_HOT))
    if data.get("status_code") != 0:
        raise SourceError(
            f"douyin hot board status_code={data.get('status_code')}（0 才是成功）"
        )
    # 榜单只有整体时间戳（active_time），没有每条时间
    published = _cn_time_to_iso(str(data.get("active_time") or ""))
    items: list[HotspotItem] = []
    for i, row in enumerate(data.get("word_list") or []):
        word = str(row.get("word") or "").strip()
        if not word:
            continue
        items.append(
            HotspotItem(
                source="douyin_hot",
                title=word,
                url=f"https://www.douyin.com/search/{quote(word)}",
                summary="",
                published_at=published,
                score=float(row.get("hot_value") or 0) or None,
                # label 的含义官方没文档化（实测见过 0/1/3/5/16），
                # 原样记下来供后续比对，**不猜**成「热/新/荐」之类
                meta={"label": row.get("label"), "rank": i + 1},
            )
        )
    return items[:limit]


def _parse_atom(xml_text: str, source_id: str) -> list[HotspotItem]:
    """arXiv Atom 解析（命名空间不能省，``findtext("title")`` 会什么都找不到）。"""
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_text)
    items: list[HotspotItem] = []
    for entry in root.findall("a:entry", ns):
        # Atom 源码常把标题/摘要断行缩进，不压空白会得到一堆碎空格
        title = re.sub(r"\s+", " ", entry.findtext("a:title", "", ns) or "").strip()
        if not title:
            continue
        summary = re.sub(r"\s+", " ", entry.findtext("a:summary", "", ns) or "").strip()
        link = entry.find("a:link[@rel='alternate']", ns)
        items.append(
            HotspotItem(
                source=source_id,
                title=title,
                url=(link.get("href") or "") if link is not None else "",
                summary=summary[:600],
                published_at=entry.findtext("a:published", None, ns),
                meta={"arxivId": (entry.findtext("a:id", "", ns) or "").strip()},
            )
        )
    return items


def fetch_arxiv_latest(limit: int = 20, category: str = "cs.AI") -> list[HotspotItem]:
    """arXiv 最新论文（官方 Atom API，免密钥）。

    2026-09-09 曾误判为「连不通」：那次用的是 ``http://`` 且没跟重定向
    （301 → https）。**https 直接请求就是 200。**
    """
    url = (
        f"{_ARXIV_API}?search_query=cat:{quote(category)}"
        f"&sortBy=submittedDate&sortOrder=descending&max_results={max(1, limit)}"
    )
    return _parse_atom(http_text(url, timeout=25.0), "arxiv_latest")


def fetch_toutiao_hot(limit: int = 50) -> list[HotspotItem]:
    """今日头条热榜（**官方**接口，免登录免密钥）。

    50 条中文热点 + ``HotValue`` + ``Label``（hot/新/沸之类）。
    官方 JSON、无需签名，是中文侧最稳的一条。
    """
    data = json.loads(http_text(_TOUTIAO_HOT))
    # 接口不返回每条时间：热榜本身就是「此刻」，按抓取时间记
    now = datetime.now(timezone.utc).isoformat()
    items: list[HotspotItem] = []
    for i, row in enumerate(data.get("data") or []):
        title = str(row.get("Title") or "").strip()
        if not title:
            continue
        items.append(
            HotspotItem(
                source="toutiao_hot",
                title=title,
                url=str(row.get("Url") or ""),
                summary=str(row.get("QueryWord") or "")[:300],
                published_at=now,
                score=float(row.get("HotValue") or 0) or None,
                meta={"label": row.get("Label"), "rank": i + 1},
            )
        )
    return items[:limit]


def fetch_newsnow(limit: int = 30, boards: list[str] | None = None) -> list[HotspotItem]:
    """NewsNow 聚合（微博 / 知乎 / 抖音 / 头条 / 百度 …）。

    一个端点顶十个源。**公共实例是社区托管**（生产建议自部署，项目开源）；
    单个榜单挂了不影响其它榜单，但会收集进错误信息。
    ``source`` 记为 ``newsnow:<board>`` —— 平台是去重与展示的一部分，
    混成一个 ``newsnow`` 会让不同平台的同名条目互相吃掉。
    """
    selected = boards or list(DEFAULT_NEWNOW_BOARDS)
    # 多榜单时按榜单**均分** limit：否则 discover 一截断就只剩第一个榜单，
    # 「聚合」名存实亡（实测 5 榜 × 30 条 → 截断 20 条后全是微博）
    per_board = max(1, limit // max(1, len(selected)))
    buckets: dict[str, list[HotspotItem]] = {}
    errors: list[str] = []
    for board in selected:
        try:
            data = json.loads(http_text(f"{_NEWNOW}?id={quote(board)}", timeout=15.0))
        except (SourceError, ValueError) as e:
            errors.append(f"{board}: {e}")
            continue
        updated = data.get("updatedTime")
        published = None
        if isinstance(updated, (int, float)) and updated > 0:
            # 毫秒时间戳（实测 1789038272400）
            published = datetime.fromtimestamp(updated / 1000, timezone.utc).isoformat()
        bucket: list[HotspotItem] = []
        for i, row in enumerate(data.get("items") or []):
            if i >= per_board:
                break
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            extra = row.get("extra") or {}
            bucket.append(
                HotspotItem(
                    source=f"newsnow:{board}",
                    title=title,
                    url=str(row.get("url") or row.get("mobileUrl") or ""),
                    summary=str(extra.get("info") or "")[:300],
                    published_at=published,
                    meta={"board": board, "rank": i + 1, "via": "newsnow"},
                )
            )
        buckets[board] = bucket
    if not any(buckets.values()) and errors:
        raise SourceError("; ".join(errors))
    # 榜单之间**交错**输出：外层 discover 只给本源几条配额，顺序填充会让
    # 配额全被第一个榜单吃掉（实测只剩微博，知乎/百度一条不剩）
    merged: list[HotspotItem] = []
    for i in range(per_board):
        for board in selected:
            bucket = buckets.get(board) or []
            if i < len(bucket):
                merged.append(bucket[i])
    return merged


def _newsnow_fetcher(board: str) -> Callable[..., list[HotspotItem]]:
    """把某个榜单固化成独立源的抓取函数。

    **为什么拆成多个源而不是一个聚合源**：``discover`` 按源分配额并在源内
    按时间排序，聚合源里「更新时间最新的那个榜」会把配额全吃掉（实测只剩
    微博，知乎/百度一条不剩）。拆开后每个榜各有一份配额，交错展示。
    """

    def fetch(limit: int = 30, **_kwargs: object) -> list[HotspotItem]:
        return fetch_newsnow(limit=limit, boards=[board])

    return fetch


# ---------------------------------------------------------------------------
# 注册表与编排


@dataclass(frozen=True)
class SourceSpec:
    """一个源的元信息 + 抓取函数。"""

    id: str
    title: str
    kind: str  # api / rss / html / browser
    needs_key: bool
    note: str
    fetch: Callable[..., list[HotspotItem]]
    #: True = 需要登录态（走 CDP 复用用户浏览器）。与 needs_key 无关的
    #: 另一种「不是装上就能跑」，必须在 list_sources 里如实暴露，否则
    #: 调用方会以为配了源就有数据。
    requires_login: bool = False


SOURCES: dict[str, SourceSpec] = {
    "huggingface_daily": SourceSpec(
        "huggingface_daily",
        "Hugging Face Daily Papers",
        "api",
        False,
        "AI 论文（英文）；社区热度排序",
        fetch_huggingface_daily,
    ),
    "github_trending": SourceSpec(
        "github_trending",
        "GitHub Trending",
        "html",
        False,
        "开源项目（英文）；抓 HTML，结构会变",
        fetch_github_trending,
    ),
    "rss": SourceSpec(
        "rss",
        "RSS/Atom 订阅",
        "rss",
        False,
        "中文科技媒体（默认少数派 + InfoQ）；HOTSPOT_RSS_FEEDS 可覆盖",
        fetch_rss,
    ),
    "douyin_hot": SourceSpec(
        "douyin_hot",
        "抖音热榜",
        "api",
        False,
        "中文社会热点 50 条 + 热度值；免登录免密钥（STEPWORK 最贴近的一条）",
        fetch_douyin_hot,
    ),
    "arxiv_latest": SourceSpec(
        "arxiv_latest",
        "arXiv 最新论文",
        "api",
        False,
        "官方 Atom API；默认 cs.AI（英文）",
        fetch_arxiv_latest,
    ),
    "toutiao_hot": SourceSpec(
        "toutiao_hot",
        "今日头条热榜",
        "api",
        False,
        "**官方**接口免登录；50 条中文热点 + HotValue（中文侧最稳）",
        fetch_toutiao_hot,
    ),
    "newsnow_weibo": SourceSpec(
        "newsnow_weibo",
        "NewsNow · 微博热搜",
        "api",
        False,
        "中文社会热点；公共实例为社区托管（开源可自部署）",
        _newsnow_fetcher("weibo"),
    ),
    "newsnow_zhihu": SourceSpec(
        "newsnow_zhihu",
        "NewsNow · 知乎热榜",
        "api",
        False,
        "中文问答/观点；适合做「争议型」选题",
        _newsnow_fetcher("zhihu"),
    ),
    "newsnow_toutiao": SourceSpec(
        "newsnow_toutiao",
        "NewsNow · 今日头条",
        "api",
        False,
        "中文资讯热点",
        _newsnow_fetcher("toutiao"),
    ),
    "newsnow_baidu": SourceSpec(
        "newsnow_baidu",
        "NewsNow · 百度热搜",
        "api",
        False,
        "中文搜索热度（与抖音热榜互补）",
        _newsnow_fetcher("baidu"),
    ),
    "newsnow_bilibili": SourceSpec(
        "newsnow_bilibili",
        "NewsNow · 哔哩哔哩",
        "api",
        False,
        "视频区热门；与抖音同属短视频参照系",
        _newsnow_fetcher("bilibili"),
    ),
    "douhot": SourceSpec(
        "douhot",
        "抖音热点宝",
        "browser",
        False,
        (
            "**需登录态**：无公开 API，走 CDP 复用用户已登录浏览器（--remote-debugging-port）。"
            "需 pip install 'stepwork-hotspot-mcp[browser]'；端点用 DOUHOT_CDP_ENDPOINT 覆盖"
        ),
        douhot_fetcher(),
        requires_login=True,
    ),
    "douhot_low_fans": SourceSpec(
        "douhot_low_fans",
        "抖音热点宝 · 低粉爆款榜",
        "browser",
        False,
        "**需登录态**（同 douhot）；低粉爆款 = 对中小账号参考价值最高的一个榜",
        douhot_fetcher("low_fans"),
        requires_login=True,
    ),
}


def _rank(item: HotspotItem) -> int:
    """取榜单名次（缺则为 0）；用于同时间同热度时的保序。"""
    raw = item.meta.get("rank")
    return raw if isinstance(raw, int) else 0


def _iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def select_sources(
    sources: list[str] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """决定这次抓哪些源、哪些跳过。

    抽成纯函数是为了**可测**：编排逻辑不该依赖网络（否则每次跑测试都要
    真抓 11 个源，20s 起跳，且网络一抖就红）。

    ``sources=None`` 时**默认跳过需登录态的源**（热点宝）：它要连浏览器、
    要用户先登录，放进全源抓取等于让「没开浏览器」变成每次调用都带一条
    错误 —— 那是噪音，不是信息。要用就显式点名。
    """
    if sources is None:
        selected = [s for s in SOURCES if not SOURCES[s].requires_login]
        skipped = [
            {"source": s, "reason": "需要登录态（CDP）；显式传 sources 才会抓"}
            for s in SOURCES
            if SOURCES[s].requires_login
        ]
        return selected, skipped
    return [s for s in sources if s in SOURCES], []


def discover(
    sources: list[str] | None = None,
    limit: int = 20,
    window_hours: int = 48,
    query: str | None = None,
) -> dict[str, Any]:
    """抓热点并按时间窗过滤、去重、排序。

    Args:
        sources: 指定源；``None`` = 全部**免登录**源（需登录的见 ``skipped``）。

    Returns:
        ``{"items", "errors"[{"source","error"}], "count", "sources",
        "skipped"[{"source","reason"}]}``。

        某个源失败**不会**让整个调用失败 —— 但一定出现在 ``errors`` 里
        （静默少给一半数据比直接报错更难查）。``skipped`` 与 ``errors`` 分开：
        前者是「这次没打算抓」，后者是「打算抓但失败了」，混在一起会让人
        以为源坏了。
    """
    selected, skipped = select_sources(sources)
    unknown = [s for s in (sources or []) if s not in SOURCES]
    if unknown:
        raise SourceError(f"unknown sources: {', '.join(unknown)}")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, window_hours))
    seen: dict[str, HotspotItem] = {}
    errors: list[dict[str, str]] = []
    # 每个源的配额。**不能**把所有条目扔进一个全局排序再截断 —— 那等于让
    #「拿抓取时刻当时间」的源（头条/抖音/榜单类）永远排在最前，其它源一条
    # 都露不出来（实测：头条 50 条吃满 40 的限额，微博/知乎/B站全被挤掉）；
    # 且不同源的 score 量纲不可比（抖音千万级 vs GitHub star 千级 vs 无分值），
    # 跨源比 score 没有意义。
    per_source = max(1, (limit + len(selected) - 1) // max(1, len(selected)))
    per_source_items: dict[str, list[HotspotItem]] = {}

    for name in selected:
        spec = SOURCES[name]
        try:
            fetched = spec.fetch(limit=limit)
        except Exception as e:  # noqa: BLE001 - 单源失败不拖垮整体
            errors.append({"source": name, "error": f"{type(e).__name__}: {e}"})
            continue
        kept: list[HotspotItem] = []
        for item in fetched:
            published = _iso_to_dt(item.published_at)
            if published is not None and published < cutoff:
                continue
            if query and query.lower() not in (item.title + item.summary).lower():
                continue
            if item.id in seen:
                continue
            seen[item.id] = item
            kept.append(item)
        # 源内部保序：时间 desc → 热度 desc → 榜单名次 asc
        kept.sort(
            key=lambda it: (
                _iso_to_dt(it.published_at) is None,
                -(_iso_to_dt(it.published_at) or datetime.now(timezone.utc)).timestamp(),
                -(it.score or 0),
                _rank(it),
            )
        )
        per_source_items[name] = kept[:per_source]

    # 交错合并（各源第 1 条 → 各源第 2 条 …）：结果才是「聚合榜」，
    # 而不是「某一个源的完整榜单 + 其它源的残渣」
    merged: list[HotspotItem] = []
    for i in range(per_source):
        for name in selected:
            bucket = per_source_items.get(name) or []
            if i < len(bucket):
                merged.append(bucket[i])

    items = merged[:limit]
    return {
        "items": [it.to_dict() for it in items],
        "errors": errors,
        "count": len(items),
        "sources": selected,
        "skipped": skipped,
    }
