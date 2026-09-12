"""热点宝连接器测试。

真跑浏览器既慢又依赖登录态，所以**解析逻辑一律用 fake driver 喂快照**覆盖，
与本项目其它源（HTML/JSON 固定件）的做法一致。真机的部分（playwright 未装
时的报错、CDP 连接）只测「错误转译」这一层。
"""

from __future__ import annotations

from typing import Any

import pytest

from stepwork_hotspot_mcp.douhot import (
    BOARDS,
    CdpDouhotDriver,
    DouhotSnapshot,
    _board_url,
    _clean_links,
    _collect_json,
    _endpoints_for,
    _find_item_lists,
    _looks_like_404,
    _to_float,
    _to_iso,
    fetch_douhot,
    iter_boards,
    parse_payloads,
    parse_snapshot,
    parse_text,
)
from stepwork_hotspot_mcp.models import SourceError


class FakeDriver:
    """只实现 :class:`DouhotDriver` 协议，喂固定快照。"""

    def __init__(
        self, snapshot: DouhotSnapshot, boards: list[dict[str, str]] | None = None
    ) -> None:
        self._snapshot = snapshot
        self._boards = boards or []
        self.calls: list[str | None] = []

    def snapshot(self, board: str | None = None) -> DouhotSnapshot:
        self.calls.append(board)
        return self._snapshot

    def list_boards(self) -> list[dict[str, str]]:
        return self._boards


class BoomDriver:
    def snapshot(self, board: str | None = None) -> DouhotSnapshot:
        raise RuntimeError("cdp refused")

    def list_boards(self) -> list[dict[str, str]]:
        raise RuntimeError("cdp refused")


# ---------------------------------------------------------------------------
# 解析：JSON 优先


def test_parse_payloads_flat_list() -> None:
    payload = {"data": {"list": [{"word": "秋天穿搭", "hot_value": 1234567}]}}
    items = parse_payloads([payload], "hotspot")
    assert len(items) == 1
    assert items[0].title == "秋天穿搭"
    assert items[0].score == 1234567.0
    assert items[0].meta["parse"] == "json"
    assert items[0].meta["board"] == "hotspot"


def test_parse_payloads_deep_nesting() -> None:
    # 接口喜欢裹 4~5 层：data.word_list[].card_list[]
    payload = {"data": {"word_list": [{"card_list": [{"title": "露营装备", "heat": "3.2w"}]}]}}
    items = parse_payloads([payload], "hotspot")
    assert [i.title for i in items] == ["露营装备"]
    assert items[0].score == 32000.0


def test_parse_payloads_alternate_key_names() -> None:
    # 字段名在不同榜之间会换（hotWord / query / name）
    payload = [{"hotWord": "甲", "score": 1}, {"query": "乙", "index": 2}, {"name": "丙"}]
    items = parse_payloads([payload], "hotspot")
    assert [i.title for i in items] == ["甲", "乙", "丙"]


def test_parse_payloads_dedupes_and_skips_garbage() -> None:
    payload = [
        {"word": "同一条"},
        {"word": "同一条"},  # 同 id（源+标题+url 相同）→ 去重
        {"no_title_here": 1},  # 无标题键 → 跳过
        {"word": "   "},  # 空白标题 → 跳过
    ]
    items = parse_payloads([payload], "hotspot")
    assert [i.title for i in items] == ["同一条"]


def test_parse_payloads_url_and_time() -> None:
    payload = [
        {
            "title": "示例",
            "share_url": "https://douhot.douyin.com/x",
            "create_time": 1_754_000_000,
        }
    ]
    items = parse_payloads([payload], "hotspot")
    assert items[0].url == "https://douhot.douyin.com/x"
    assert items[0].published_at is not None
    assert items[0].published_at.startswith("2025-")


def test_parse_snapshot_falls_back_to_text() -> None:
    snap = DouhotSnapshot(
        board="hotspot", payloads=[], text="热点宝\n秋天穿搭\n12.3w\n露营\n\n更多\n"
    )
    items = parse_snapshot(snap)
    assert [i.title for i in items] == ["秋天穿搭", "露营"]
    assert items[0].score == 123000.0
    assert items[0].meta["parse"] == "text-fallback"


def test_parse_text_skips_navigation_noise() -> None:
    text = "\n".join(["首页", "榜单", "筛选", "1", "真实选题标题", "5,678", "更多", ""])
    items = parse_text(text, "hotspot")
    assert [i.title for i in items] == ["真实选题标题"]
    assert items[0].score == 5678.0


def test_parse_text_keeps_item_without_heat() -> None:
    # 没有热度值也要收：丢掉选题比少一个数字严重
    items = parse_text("没有热度的标题\n下一条\n", "hotspot")
    assert [i.title for i in items] == ["没有热度的标题", "下一条"]


def test_find_item_lists_ignores_scalar_lists() -> None:
    assert _find_item_lists({"a": [1, 2, 3]}) == []
    assert _find_item_lists([{"word": "x"}]) == [[{"word": "x"}]]


# ---------------------------------------------------------------------------
# 真机验收的回归钉（2026-09-13）
#
# 这三个缺陷的共同点：**都不报错**。微前端对未知路由返回 200，而启发式对
# 「非空」就放行 —— 于是 404 页上的包注册表被当成榜单数据走完整条流水线。
# 所以这些测试专门钉「失败要响」这件事。
# ---------------------------------------------------------------------------


def test_board_url_uses_verified_routes() -> None:
    """``board`` 短名必须落到**实测过**的完整 URL，而不是拼出来的路径。

    上一版 ``_board_url`` 把默认榜拼成 ``/hotspot``，真路由却是
    ``/square/hotspot``（见 :data:`BOARDS`）。
    """
    assert _board_url(None) == BOARDS["hotspot"]
    assert _board_url("hotword") == BOARDS["hotword"]
    assert "/square/hotspot" in _board_url(None)
    # 完整 URL 与站内路径仍放行 —— 后者是逃生舱，落到 404 时会被 _looks_like_404 拦住
    assert _board_url("https://x.example/y") == "https://x.example/y"
    assert _board_url("square/trend") == "https://douhot.douyin.com/square/trend"


def test_board_ids_round_trip_through_board_url() -> None:
    """``list_boards()`` 给的 ``id`` 喂回 ``_board_url`` 必须回到**同一页**。

    这条不变量正是上一版 404 的根因：``id`` 取 URL 最后一段（``hotspot``），
    而 ``_board_url("hotspot")`` 拼出 ``/hotspot`` —— 两边各自看都对，拼起来
    就是 404，而且**不报错**。往返一次比对照两边代码可靠。
    """
    links = _clean_links(
        [
            {"href": "https://douhot.douyin.com/square/hotspot", "text": "榜单聚合"},
            {"href": "/calendar", "text": "活动日历"},
            {"href": "https://douhot.douyin.com/square/trend", "text": "内容词趋势"},
        ]
    )
    assert [link["id"] for link in links] == [
        "square/hotspot",
        "calendar",
        "square/trend",
    ]
    for link in links:
        assert _board_url(link["id"]) == link["url"]


#: 实测的 404 页（微前端对未知路由返回 **200** + 这张页）
_404_BODY = (
    "我的数据活动日历榜单聚合内容词趋势数据观测创作者服务\n"
    "发布视频\n"
    "404\n"
    "｜帐号授权协议｜用户服务协议｜隐私政策｜帐号找回｜联系我们｜\n"
    "2026 © 抖音 ｜ 京ICP备16016397号-3\n"
)


def test_detects_the_real_404_page() -> None:
    assert _looks_like_404(_404_BODY)


def test_does_not_call_a_normal_page_404() -> None:
    """正文里出现「404」这三个数字很常见（讲知识的选题），不能误杀。"""
    normal = "\n".join(["榜单聚合", "视频榜", "HTTP 404 是什么", "播放量", "5634.8万"])
    assert not _looks_like_404(normal)


class _FakeResponse:
    """``_collect_json`` 只用到 ``url`` / ``headers`` / ``json()`` 三样。"""

    def __init__(self, url: str, body: object, ctype: str = "application/json") -> None:
        self.url = url
        self.headers = {"content-type": ctype}
        self._body = body

    def json(self) -> object:
        return self._body


#: 实测：**404 页也会拉这个** —— 微前端包注册表，形状恰好是「带 name 的 dict 列表」
_NOISE_URL = (
    "https://kura-cloud.zijieapi.com/api/open/registries/"
    "byted-douhot/envs/production/packages?region=cn"
)
_NOISE_REGISTRY = {
    "code": 0,
    "message": "success",
    "data": [
        {"name": "@byted-douhot/micro", "remotejs": "https://lf-cdn-tos.bytescm.com/x.js"}
    ],
}

#: 实测的真实榜单响应（视频榜），形状取自登录后的真响应
_REAL_URL = (
    "https://douhot.douyin.com/douhot/v1/material/video_billboard?msToken=&X-Bogus=abc"
)
_REAL_VIDEO_BILLBOARD = {
    "code": 0,
    "data": {
        "page": {"page": 1, "page_size": 10, "total": 1000},
        "objs": [{"item_id": "7683324387718370486", "item_title": "抖音推流的核心规则"}],
    },
}


def test_response_gate_is_what_stops_the_junk() -> None:
    """**闸门自检**：先证明「没有闸门时噪声真会变成条目」，再证明闸门挡住了它。

    少了前半句，这个测试就只是「断言了一个常量」—— 哪天噪声换个形状、启发式
    不再收它了，测试照样绿，而我们会以为闸门在起作用。
    """
    junk = parse_payloads([_NOISE_REGISTRY], "hotspot")
    assert [i.title for i in junk] == ["@byted-douhot/micro"], (
        "噪声本身必须仍能被启发式解析成条目，否则这条测试什么都没测到"
    )

    sink: list[object] = []
    _collect_json(_FakeResponse(_NOISE_URL, _NOISE_REGISTRY), sink)
    assert sink == [], "别的域（包注册表 / 问卷）一律不进"
    _collect_json(_FakeResponse(_REAL_URL, _REAL_VIDEO_BILLBOARD), sink)
    assert sink == [_REAL_VIDEO_BILLBOARD]
    assert parse_payloads(sink, "hotspot")[0].title == "抖音推流的核心规则"


def test_response_gate_skips_its_own_error_responses() -> None:
    """同一前缀下的失败响应也不算数据：``code != 0`` 是错误。"""
    sink: list[object] = []
    body = {"code": 500, "data": {"objs": [{"item_title": "不该出现"}]}}
    _collect_json(_FakeResponse(_REAL_URL, body), sink)
    assert sink == []


#: 实测：``/square/trend``（内容词趋势）**同时也会打搜索榜接口**。
_SEARCH_URL = (
    "https://douhot.douyin.com/douhot/v1/dashboard/hot_search/query_list?msToken=&X-Bogus=abc"
)
_WORD_URL = (
    "https://douhot.douyin.com/douhot/v1/dashboard/hot_word/query_list?msToken=&X-Bogus=abc"
)


def test_endpoint_gate_keeps_boards_apart() -> None:
    """**词趋势榜不许把搜索词当成自己的内容** —— 这道闸门修的正是这个。

    两个榜的条目长得一模一样（都是抖音的话题短语、都能被 ``key_word`` 认出来），
    所以**光看输出发现不了混了**：验收时眼看着「内容词趋势」返回了「助眠 / 范冰冰」
    才查出来 —— 那是搜索榜的数据，被同一个页面顺手打出来了。

    前半句同样是必须的：先证明这份响应**本身是合格数据**（前缀、形状都过），
    否则「被挡住」可能只是因为别的原因坏了，测试就测错了地方。
    """
    body = {"code": 0, "data": {"objs": [{"key_word": "助眠", "search_score": 3063424}]}}

    sink: list[object] = []
    _collect_json(_FakeResponse(_SEARCH_URL, body), sink)
    assert sink == [body], "不带端点筛选时，搜索榜响应是会被收下的 —— 所以必须靠闸门挡"

    sink = []
    _collect_json(_FakeResponse(_SEARCH_URL, body), sink, _endpoints_for("hotword"))
    assert sink == [], "词趋势榜收到搜索榜接口，必须丢掉"

    sink = []
    _collect_json(_FakeResponse(_WORD_URL, body), sink, _endpoints_for("hotword"))
    assert sink == [body], "该榜自己的端点必须照收"


def test_unknown_board_is_not_filtered() -> None:
    """不认识 ``board`` 就**不筛**（``None``）—— 筛了反而会把数据筛光。

    未知值是逃生舱的常见形态：用户可能填了一条新路径或完整 URL。此时宁可
    照收全部 ``/douhot/v1/``，也不要因为「不在已知表里」而返回空。
    """
    assert _endpoints_for("hotspot") is not None
    assert _endpoints_for("hotword") == ("dashboard/hot_word/query_list",)
    assert _endpoints_for(None) == _endpoints_for("hotspot"), "空 board 回退到默认榜"
    assert _endpoints_for("brand-new-board") is None

    body = {"code": 0, "data": {"objs": [{"key_word": "助眠"}]}}
    sink: list[object] = []
    _collect_json(_FakeResponse(_SEARCH_URL, body), sink, _endpoints_for("brand-new-board"))
    assert sink == [body], "不筛 = 照收"


#: 实测的三类榜单条目（**字段名与取值照抄 2026-09-13 登录后的真响应**）
_REAL_OBJS: list[tuple[str, dict[str, Any], str, float]] = [
    (
        "视频榜",
        {
            "item_id": "7683324387718370486",
            "item_title": "抖音推流的核心规则",
            "item_cover_url": "https://p11-sign.douyinpic.com/x",
            "nick_name": "暴躁辣椒",
            "fans_cnt": 442,
            "play_cnt": 123083,
            "publish_time": 1788913365,
            "score": 572510,
        },
        "抖音推流的核心规则",
        572510.0,
    ),
    (
        "话题榜",
        {
            "challenge_id": "1590289409301517",
            "challenge_name": "琵琶曲",
            "play_cnt": 5593922784,
            "publish_cnt": 287402,
            "score": 10389252,
            "create_time": 1516618165,
            "real_rank": 1,
        },
        "琵琶曲",
        10389252.0,
    ),
    (
        "搜索榜",
        {"key_word": "助眠", "search_score": 3063424},
        "助眠",
        3063424.0,
    ),
]


@pytest.mark.parametrize(("label", "objs", "want_title", "want_score"), _REAL_OBJS)
def test_parses_the_real_board_shapes(
    label: str, objs: dict[str, Any], want_title: str, want_score: float
) -> None:
    """**真榜单的字段名必须被认出来** —— 这条不来自想象，来自登录后的真响应。

    上一版 ``_TITLE_KEYS`` 里没有 ``item_title`` / ``challenge_name`` /
    ``key_word``，``_VALUE_KEYS`` 里没有 ``search_score``。也就是说**路由全对、
    响应也拦到了，三个真榜单依然一条都解析不出来** —— 而唯一能解析出来的，
    恰好是 404 页上那些噪声（它们的字段叫 ``name``）。
    这一条 + :func:`test_response_gate_is_what_stops_the_junk` 合起来才是完整的成因链。
    """
    payload = {"code": 0, "data": {"objs": [objs]}}
    items = parse_payloads([payload], "hotspot")
    assert [i.title for i in items] == [want_title], label
    assert items[0].score == want_score, label


# ---------------------------------------------------------------------------
# 数值/时间归一


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("12.3w", 123_000.0),
        ("1.2万", 12_000.0),
        ("3k", 3_000.0),
        ("1.5亿", 150_000_000.0),
        ("4,567", 4567.0),
        (89, 89.0),
        ("", None),
        ("abc", None),
        (None, None),
        (True, None),
    ],
)
def test_to_float(raw: Any, want: float | None) -> None:
    assert _to_float(raw) == want


def test_to_iso_millis_and_seconds() -> None:
    assert _to_iso(1_754_000_000_000) == _to_iso(1_754_000_000)
    assert _to_iso(1_754_000_000) is not None
    assert _to_iso(12) is None  # 明显不是时间戳
    assert _to_iso("2026-09-10 12:00:00") == "2026-09-10 12:00:00"
    assert _to_iso(None) is None


# ---------------------------------------------------------------------------
# fetch_douhot 编排


def test_fetch_douhot_uses_driver_and_limits() -> None:
    snap = DouhotSnapshot(
        board="hotspot",
        payloads=[{"data": {"list": [{"word": f"话题{i}", "hot_value": i} for i in range(10)]}}],
    )
    driver = FakeDriver(snap)
    items = fetch_douhot(3, driver=driver)
    assert len(items) == 3
    assert driver.calls == [None]
    # 按源内顺序（热度已按 i 递增 → 取前 3 是最小三条，符合「原序截断」）
    assert [i.title for i in items] == ["话题0", "话题1", "话题2"]


def test_fetch_douhot_passes_board() -> None:
    driver = FakeDriver(DouhotSnapshot(board="hotword", payloads=[{"list": [{"word": "x"}]}]))
    items = fetch_douhot(5, driver=driver, board="hotword")
    assert items[0].meta["board"] == "hotword"
    assert driver.calls == ["hotword"]


def test_fetch_douhot_empty_raises_with_login_hint() -> None:
    driver = FakeDriver(DouhotSnapshot(board="hotspot"))
    with pytest.raises(SourceError, match="未登录"):
        fetch_douhot(5, driver=driver)


def test_fetch_douhot_wraps_driver_error() -> None:
    with pytest.raises(SourceError, match="remote-debugging-port"):
        fetch_douhot(5, driver=BoomDriver())


def test_iter_boards_yields_driver_boards() -> None:
    boards = [
        {
            "id": "square/hotspot",
            "title": "榜单聚合",
            "url": "https://douhot.douyin.com/square/hotspot",
        }
    ]
    assert list(iter_boards(FakeDriver(DouhotSnapshot("hotspot"), boards))) == boards


# ---------------------------------------------------------------------------
# CDP driver 的构造与错误转译（不真连浏览器）


def test_cdp_driver_defaults() -> None:
    driver = CdpDouhotDriver()
    assert driver._endpoint == "http://127.0.0.1:9222"
    assert driver._settle_ms == 3000


def test_cdp_driver_accepts_custom_endpoint() -> None:
    driver = CdpDouhotDriver("http://127.0.0.1:9333", settle_ms=10)
    assert driver._endpoint == "http://127.0.0.1:9333"
    assert driver._settle_ms == 10


def test_missing_playwright_error_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    """没装 playwright 时，错误必须指名安装命令，而不是 ImportError 裸奔。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("playwright"):
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SourceError, match=r"stepwork-hotspot-mcp\[browser\]"):
        fetch_douhot(5, driver=CdpDouhotDriver())


# ---------------------------------------------------------------------------
# 注册表编排：需登录的源默认不参与全源抓取


def test_select_sources_default_skips_login_sources() -> None:
    from stepwork_hotspot_mcp.sources import select_sources

    selected, skipped = select_sources(None)
    assert "douhot" not in selected
    assert "toutiao_hot" in selected
    assert {s["source"] for s in skipped} == {"douhot", "douhot_hotword"}
    assert "需要登录态" in skipped[0]["reason"]


def test_select_sources_explicit_includes_login_sources() -> None:
    from stepwork_hotspot_mcp.sources import select_sources

    selected, skipped = select_sources(["douhot", "toutiao_hot"])
    assert selected == ["douhot", "toutiao_hot"]
    assert skipped == []


def test_source_spec_exposes_requires_login() -> None:
    from stepwork_hotspot_mcp.sources import SOURCES

    assert SOURCES["douhot"].requires_login is True
    assert SOURCES["douhot"].kind == "browser"
    assert SOURCES["toutiao_hot"].requires_login is False


def test_source_id_must_match_registry_key() -> None:
    """条目 ``source`` 必须与注册表 key 一致。

    下游按 ``source`` 值过滤与分组做源内分位。曾经两个热点宝榜都写
    ``douhot`` —— ``sources=["douhot_hotword"]`` 会静默返回空
    （不报错，只是永远没数据，最难查的那种）。
    """
    from stepwork_hotspot_mcp.sources import SOURCES

    snap = DouhotSnapshot(board="hotword", payloads=[{"list": [{"word": "话题"}]}])
    driver = FakeDriver(snap)
    hotword = fetch_douhot(5, driver=driver, board="hotword", source_id="douhot_hotword")
    default = fetch_douhot(5, driver=driver, board="hotword")

    assert hotword[0].source == "douhot_hotword"
    assert hotword[0].source in SOURCES
    assert default[0].source == "douhot"
    # 不同 source ⇒ 不同 id，两榜不会互相去重掉
    assert hotword[0].id != default[0].id


def test_registry_fetchers_declare_matching_source_id() -> None:
    """注册表里每个热点宝源的 fetch，声明的 source 名必须等于它的 key。

    这是「防复发」而不是「防实现」：source 名写在闭包里，人很容易只改
    注册表 key 而忘了改 source —— 结果就是按 key 过滤永远拿到空。
    """
    from stepwork_hotspot_mcp.sources import SOURCES

    for key in ("douhot", "douhot_hotword"):
        assert getattr(SOURCES[key].fetch, "source_id", None) == key
