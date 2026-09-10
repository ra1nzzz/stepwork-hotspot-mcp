"""热点宝连接器测试。

真跑浏览器既慢又依赖登录态，所以**解析逻辑一律用 fake driver 喂快照**覆盖，
与本项目其它源（HTML/JSON 固定件）的做法一致。真机的部分（playwright 未装
时的报错、CDP 连接）只测「错误转译」这一层。
"""

from __future__ import annotations

from typing import Any

import pytest

from stepwork_hotspot_mcp.douhot import (
    CdpDouhotDriver,
    DouhotSnapshot,
    _find_item_lists,
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
    driver = FakeDriver(DouhotSnapshot(board="low_fans", payloads=[{"list": [{"word": "x"}]}]))
    items = fetch_douhot(5, driver=driver, board="low_fans")
    assert items[0].meta["board"] == "low_fans"
    assert driver.calls == ["low_fans"]


def test_fetch_douhot_empty_raises_with_login_hint() -> None:
    driver = FakeDriver(DouhotSnapshot(board="hotspot"))
    with pytest.raises(SourceError, match="未登录"):
        fetch_douhot(5, driver=driver)


def test_fetch_douhot_wraps_driver_error() -> None:
    with pytest.raises(SourceError, match="remote-debugging-port"):
        fetch_douhot(5, driver=BoomDriver())


def test_iter_boards_yields_driver_boards() -> None:
    boards = [{"id": "hotspot", "title": "热点榜", "url": "https://douhot.douyin.com/hotspot"}]
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
    assert {s["source"] for s in skipped} == {"douhot", "douhot_low_fans"}
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
    ``douhot`` —— ``sources=["douhot_low_fans"]`` 会静默返回空
    （不报错，只是永远没数据，最难查的那种）。
    """
    from stepwork_hotspot_mcp.sources import SOURCES

    snap = DouhotSnapshot(board="low_fans", payloads=[{"list": [{"word": "话题"}]}])
    driver = FakeDriver(snap)
    low = fetch_douhot(5, driver=driver, board="low_fans", source_id="douhot_low_fans")
    default = fetch_douhot(5, driver=driver, board="low_fans")

    assert low[0].source == "douhot_low_fans"
    assert low[0].source in SOURCES
    assert default[0].source == "douhot"
    # 不同 source ⇒ 不同 id，两榜不会互相去重掉
    assert low[0].id != default[0].id


def test_registry_fetchers_declare_matching_source_id() -> None:
    """注册表里每个热点宝源的 fetch，声明的 source 名必须等于它的 key。

    这是「防复发」而不是「防实现」：source 名写在闭包里，人很容易只改
    注册表 key 而忘了改 source —— 结果就是按 key 过滤永远拿到空。
    """
    from stepwork_hotspot_mcp.sources import SOURCES

    for key in ("douhot", "douhot_low_fans"):
        assert getattr(SOURCES[key].fetch, "source_id", None) == key
