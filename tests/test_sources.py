"""连接器测试（离线：固定件 + 假抓取函数，不打网络）。

为什么用固定件而不是真打网络：GitHub Trending 是抓 HTML，结构一变就挂。
固定件把「今天长这样」钉住，真挂了能立刻看出是**对方改版**还是**我们改错**。
真网络的可用性另有一份手动探测结论，写在 sources.py 的 docstring 里。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from stepwork_hotspot_mcp.models import HotspotItem, SourceError
from stepwork_hotspot_mcp.sources import (
    SOURCES,
    _parse_gh_trending,
    _parse_rss,
    discover,
    fetch_arxiv_latest,
    fetch_douyin_hot,
    fetch_huggingface_daily,
    fetch_newsnow,
    fetch_toutiao_hot,
    strip_html,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_strip_html_removes_tags_and_entities() -> None:
    assert strip_html("<p>a &amp; b<br/></p>") == "a & b"
    assert strip_html("x&nbsp;y") == "x y"


def test_parse_rss_reads_title_link_summary_and_date() -> None:
    items = _parse_rss((FIXTURES / "sspai.xml").read_text(encoding="utf-8"), "rss")
    assert len(items) == 2
    first = items[0]
    assert first.title.startswith("用 AI 整理我的读书笔记")
    assert "&" in first.title  # &amp; 已还原
    assert first.url == "https://sspai.com/post/114366"
    assert "加粗" in first.summary and "<p>" not in first.summary
    assert first.published_at is not None and first.published_at.endswith("+00:00")
    # 没 pubDate 的条目不能崩，且 published_at 为 None（上游按「未知时间」处理）
    assert items[1].published_at is None


def test_parse_github_trending_extracts_repo_stars_language() -> None:
    html = (FIXTURES / "github_trending.html").read_text(encoding="utf-8")
    items = _parse_gh_trending(html, limit=10)
    assert [i.title for i in items] == ["ayghri/i-have-adhd", "Tencent/teamai-cli"]
    assert items[0].url == "https://github.com/ayghri/i-have-adhd"
    assert items[0].score == 4624.0  # "4,624 stars today"
    assert items[0].meta["language"] == "Python"
    assert items[0].meta["rank"] == 1
    assert "burying the answer" in items[0].summary


def test_parse_github_trending_survives_layout_change() -> None:
    """结构变了应当表现为「没数据」，而不是异常（上层会把它列进 errors）。"""
    assert _parse_gh_trending("<html><body>redesigned</body></html>", 10) == []


def test_huggingface_parses_nested_paper_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    import stepwork_hotspot_mcp.sources as src

    payload = [
        {
            "title": "A Paper",
            "summary": "**bold** summary",
            "publishedAt": _iso(96),  # 论文发布日（几天前）
            "paper": {
                "id": "2609.06245",
                "title": "A Paper",
                "summary": "**bold** summary",
                "upvotes": 42,
                "authors": [{"name": "Someone"}],
                "publishedAt": _iso(96),
                "submittedOnDailyAt": _iso(2),  # 上今日榜的时刻
            },
        }
    ]
    monkeypatch.setattr(src, "http_text", lambda url, timeout=20.0: json.dumps(payload))
    items = fetch_huggingface_daily(limit=5)
    assert len(items) == 1
    assert items[0].url == "https://huggingface.co/papers/2609.06245"
    assert items[0].score == 42.0
    # HF 摘要是 Markdown 不是 HTML：不要当标签剥（剥了信息就没了）
    assert items[0].summary == "**bold** summary"
    # 时间取「上今日榜」而非论文发布日：用发布日会被时间窗过滤光
    assert items[0].published_at == _iso(2)
    assert items[0].meta["paperPublishedAt"] == _iso(96)


# --- discover 编排 ---------------------------------------------------------


def _patch_sources(monkeypatch: pytest.MonkeyPatch, **fetchers: object) -> None:
    import stepwork_hotspot_mcp.sources as src

    for name, fn in fetchers.items():
        monkeypatch.setitem(SOURCES, name, src.SourceSpec(name, name, "api", False, "", fn))  # type: ignore[arg-type]


def test_discover_filters_by_window_and_dedups(monkeypatch: pytest.MonkeyPatch) -> None:
    old = HotspotItem(source="rss", title="两天前", published_at=_iso(50))
    fresh = HotspotItem(source="rss", title="一小时前", published_at=_iso(1), score=3)
    dup = HotspotItem(source="rss", title="一小时前", published_at=_iso(1), score=9)

    def feed(**_kwargs: object) -> list[HotspotItem]:
        return [old, fresh, dup]

    _patch_sources(monkeypatch, rss=feed)
    out = discover(sources=["rss"], limit=20, window_hours=48)
    assert out["count"] == 1
    assert out["items"][0]["title"] == "一小时前"
    assert out["errors"] == []


def test_discover_keeps_items_without_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    def feed(**_kwargs: object) -> list[HotspotItem]:
        return [HotspotItem(source="rss", title="没时间的条目")]

    _patch_sources(monkeypatch, rss=feed)
    out = discover(sources=["rss"], window_hours=1)
    assert out["count"] == 1


def test_discover_query_matches_title_or_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    def feed(**_kwargs: object) -> list[HotspotItem]:
        return [
            HotspotItem(source="rss", title="讲 AI 的一条"),
            HotspotItem(source="rss", title="无关", summary="完全不沾边"),
        ]

    _patch_sources(monkeypatch, rss=feed)
    out = discover(sources=["rss"], query="ai")
    assert [i["title"] for i in out["items"]] == ["讲 AI 的一条"]


def test_source_failure_is_reported_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_kwargs: object) -> list[HotspotItem]:
        raise RuntimeError("HTTP 403")

    def fine(**_kwargs: object) -> list[HotspotItem]:
        return [HotspotItem(source="rss", title="正常的一条")]

    _patch_sources(monkeypatch, github_trending=boom, rss=fine)
    out = discover(sources=["github_trending", "rss"])
    assert out["count"] == 1
    # 静默少给一半数据比直接报错更难查 —— 失败必须出现在 errors 里
    assert out["errors"][0]["source"] == "github_trending"
    assert "403" in out["errors"][0]["error"]


def test_unknown_source_is_rejected() -> None:
    with pytest.raises(Exception, match="unknown sources"):
        discover(sources=["weibo_hot_search"])


def test_douyin_hot_parses_words_and_beijing_time(monkeypatch: pytest.MonkeyPatch) -> None:
    import stepwork_hotspot_mcp.sources as src

    payload = json.loads((FIXTURES / "douyin_hot.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(src, "http_text", lambda url, timeout=20.0: json.dumps(payload))
    items = fetch_douyin_hot(limit=50)

    assert len(items) == 3
    assert items[0].title == "青岛货轮火灾已造成20人遇难"
    assert items[0].score == 11629010.0
    assert items[0].url.startswith("https://www.douyin.com/search/")
    assert items[0].meta["rank"] == 1
    # active_time 是北京时间且不带时区：不补 +08:00 会被当成 UTC，
    # 48 小时窗会整体偏 8 小时（少掉三分之一条目）
    assert items[0].published_at == "2026-09-10T18:57:21+08:00"


def test_douyin_hot_rejects_nonzero_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import stepwork_hotspot_mcp.sources as src

    monkeypatch.setattr(
        src, "http_text", lambda url, timeout=20.0: json.dumps({"status_code": 8})
    )
    with pytest.raises(SourceError, match="status_code=8"):
        fetch_douyin_hot()


def test_arxiv_parses_atom_with_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    import stepwork_hotspot_mcp.sources as src

    xml = (FIXTURES / "arxiv.xml").read_text(encoding="utf-8")
    monkeypatch.setattr(src, "http_text", lambda url, timeout=20.0: xml)
    items = fetch_arxiv_latest(limit=10)

    assert len(items) == 2
    # 标题里的换行要压成单空格（Atom 源码常断行）
    assert items[0].title == (
        "A Challenging Benchmark for Fine-Grained Image Difference Identification"
    )
    assert items[0].url == "http://arxiv.org/abs/2609.06245v1"
    assert items[0].meta["arxivId"] == "http://arxiv.org/abs/2609.06245v1"
    assert items[0].published_at == "2026-09-09T20:00:00Z"


def test_toutiao_hot_reads_title_url_hotvalue(monkeypatch: pytest.MonkeyPatch) -> None:
    import stepwork_hotspot_mcp.sources as src

    payload = json.loads((FIXTURES / "toutiao_hot.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(src, "http_text", lambda url, timeout=20.0: json.dumps(payload))
    items = fetch_toutiao_hot(limit=10)

    assert len(items) == 2
    assert items[0].title == "习近平对青岛货轮火灾作出重要指示"
    assert items[0].url.endswith("7683828786948784678")
    assert items[0].score == 7268496.0
    assert items[0].meta["label"] == "hot"
    assert items[0].meta["rank"] == 1
    # 接口不给每条时间 → 按抓取时间记（否则时间窗会把它全过滤掉）
    assert items[0].published_at is not None


def test_newsnow_scopes_source_by_board(monkeypatch: pytest.MonkeyPatch) -> None:
    import stepwork_hotspot_mcp.sources as src

    payload = json.loads((FIXTURES / "newsnow_weibo.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(src, "http_text", lambda url, timeout=20.0: json.dumps(payload))
    items = fetch_newsnow(limit=10, boards=["weibo"])

    assert len(items) == 2
    # source 必须同时满足两点：带榜单名（不同平台同名条目不互相吃掉），
    # 且**与 SOURCES 的 key 一致**（下游按 source 值过滤/分组做源内分位）
    assert items[0].source == "newsnow_weibo"
    assert items[0].source in SOURCES
    assert items[0].title == "青岛货轮火灾造成重大人员伤亡"
    assert items[0].meta["board"] == "weibo"
    # extra.info 是热度文案，进 summary
    assert items[0].summary == "1162万"
    assert items[0].published_at == "2026-09-10T11:04:32.400000+00:00"


def test_newsnow_splits_limit_across_boards(monkeypatch: pytest.MonkeyPatch) -> None:
    """多榜单必须均分 limit，否则 discover 一截断就只剩第一个榜单。"""
    import stepwork_hotspot_mcp.sources as src

    payload = json.loads((FIXTURES / "newsnow_weibo.json").read_text(encoding="utf-8"))
    calls: list[str] = []

    def fake(url: str, timeout: float = 20.0) -> str:
        calls.append(url)
        return json.dumps(payload)

    monkeypatch.setattr(src, "http_text", fake)
    items = fetch_newsnow(limit=4, boards=["weibo", "zhihu"])
    # 4 条 / 2 榜 = 每榜 2 条；固定件只有 2 条，故各取 2
    assert len(calls) == 2
    assert len(items) == 4
    assert {i.meta["board"] for i in items} == {"weibo", "zhihu"}
    # 交错输出：外层配额很小时也能每个榜都露面，而不是被第一个榜吃满
    assert [i.meta["board"] for i in items] == ["weibo", "zhihu", "weibo", "zhihu"]


def test_rank_keeps_board_order_within_same_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """热榜的名次就是重要性：时间/热度都相同时必须按 rank 保序。"""
    now = datetime.now(timezone.utc)

    def feed(**_kwargs: object) -> list[HotspotItem]:
        return [
            HotspotItem(source="x", title="第三", published_at=now.isoformat(), meta={"rank": 3}),
            HotspotItem(source="x", title="第一", published_at=now.isoformat(), meta={"rank": 1}),
            HotspotItem(source="x", title="第二", published_at=now.isoformat(), meta={"rank": 2}),
        ]

    _patch_sources(monkeypatch, rss=feed)
    out = discover(sources=["rss"], limit=10, window_hours=48)
    assert [i["title"] for i in out["items"]] == ["第一", "第二", "第三"]


def test_every_source_gets_a_share_of_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """全局排序 + 截断会让「用抓取时刻当时间」的源吃满限额，其它源一条不剩。

    实测事故：头条（published_at=now）50 条吃满 limit=40，微博/知乎/B站
    全部被挤掉。故改为按源配额 + 交错。
    """
    now = datetime.now(timezone.utc)
    later = (now.replace(microsecond=0)).isoformat()

    def big(**_kwargs: object) -> list[HotspotItem]:
        return [
            HotspotItem(source="a", title=f"a{i}", published_at=later, meta={"rank": i})
            for i in range(1, 51)
        ]

    def small(**_kwargs: object) -> list[HotspotItem]:
        return [HotspotItem(source="b", title="b1", published_at=_iso(3))]

    _patch_sources(monkeypatch, rss=big, github_trending=small)
    out = discover(sources=["rss", "github_trending"], limit=10, window_hours=48)
    titles = [i["title"] for i in out["items"]]
    # b1 必须出现，且不能全是 a
    assert "b1" in titles
    assert sum(1 for t in titles if t.startswith("a")) <= 9


def test_items_from_different_sources_interleave(monkeypatch: pytest.MonkeyPatch) -> None:
    """交错合并：前几条应当来自不同源，而不是第一个源的完整榜单。"""
    now = datetime.now(timezone.utc).isoformat()

    def feed_a(**_kwargs: object) -> list[HotspotItem]:
        return [
            HotspotItem(source="a", title=f"a{i}", published_at=now, meta={"rank": i})
            for i in range(1, 6)
        ]

    def feed_b(**_kwargs: object) -> list[HotspotItem]:
        return [
            HotspotItem(source="b", title=f"b{i}", published_at=now, meta={"rank": i})
            for i in range(1, 6)
        ]

    _patch_sources(monkeypatch, rss=feed_a, github_trending=feed_b)
    out = discover(sources=["rss", "github_trending"], limit=10, window_hours=48)
    assert [i["title"] for i in out["items"]][:4] == ["a1", "b1", "a2", "b2"]


def test_item_id_is_stable_and_source_scoped() -> None:
    a = HotspotItem(source="rss", title="同一条", url="https://x/1")
    b = HotspotItem(source="rss", title="同一条", url="https://x/1")
    c = HotspotItem(source="github_trending", title="同一条", url="https://x/1")
    assert a.id == b.id
    assert a.id != c.id
