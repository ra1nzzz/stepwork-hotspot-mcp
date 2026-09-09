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

from stepwork_hotspot_mcp.models import HotspotItem
from stepwork_hotspot_mcp.sources import (
    SOURCES,
    _parse_gh_trending,
    _parse_rss,
    discover,
    fetch_huggingface_daily,
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


def test_item_id_is_stable_and_source_scoped() -> None:
    a = HotspotItem(source="rss", title="同一条", url="https://x/1")
    b = HotspotItem(source="rss", title="同一条", url="https://x/1")
    c = HotspotItem(source="github_trending", title="同一条", url="https://x/1")
    assert a.id == b.id
    assert a.id != c.id
