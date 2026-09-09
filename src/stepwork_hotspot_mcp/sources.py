"""热点连接器（三个，全部免密钥）。

选择标准是**能否在这台机器上真的拿到数据**，不是「文档上写得好」。
2026-09-09 实测：

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

**结论先说**：中文「热搜榜」这条路线在本机**拿不到**（无官方 RSS + 反爬 +
第三方镜像不可达）。能稳定拿到的是「AI 论文 / 开源项目 / 中文科技媒体
RSS」—— 这已经决定了产品的上游形态：不是「追社会热点」，而是「追技术圈
正在讨论什么」。
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

from .models import HotspotItem, SourceError

_UA = "stepwork-hotspot-mcp/0.1 (+https://github.com/ra1nzzz/stepwork-hotspot-mcp)"

#: 默认 RSS 源（中文科技/效率）。用 ``HOTSPOT_RSS_FEEDS`` 覆盖（逗号分隔）
DEFAULT_RSS_FEEDS = [
    "https://sspai.com/feed",
]

_HF_DAILY = "https://huggingface.co/api/daily_papers"
_GH_TRENDING = "https://github.com/trending"


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


# ---------------------------------------------------------------------------
# 注册表与编排


@dataclass(frozen=True)
class SourceSpec:
    """一个源的元信息 + 抓取函数。"""

    id: str
    title: str
    kind: str  # api / rss / html
    needs_key: bool
    note: str
    fetch: Callable[..., list[HotspotItem]]


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
        "中文科技媒体（默认少数派）；HOTSPOT_RSS_FEEDS 可覆盖",
        fetch_rss,
    ),
}


def _iso_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def discover(
    sources: list[str] | None = None,
    limit: int = 20,
    window_hours: int = 48,
    query: str | None = None,
) -> dict[str, Any]:
    """抓热点并按时间窗过滤、去重、排序。

    Returns:
        ``{"items": [...], "errors": [{"source", "error"}], "sources": [...]}``。
        某个源失败**不会**让整个调用失败 —— 但一定出现在 ``errors`` 里
        （静默少给一半数据比直接报错更难查）。
    """
    selected = [s for s in (sources or list(SOURCES)) if s in SOURCES]
    unknown = [s for s in (sources or []) if s not in SOURCES]
    if unknown:
        raise SourceError(f"unknown sources: {', '.join(unknown)}")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, window_hours))
    seen: dict[str, HotspotItem] = {}
    errors: list[dict[str, str]] = []

    for name in selected:
        spec = SOURCES[name]
        try:
            fetched = spec.fetch(limit=limit)
        except Exception as e:  # noqa: BLE001 - 单源失败不拖垮整体
            errors.append({"source": name, "error": f"{type(e).__name__}: {e}"})
            continue
        for item in fetched:
            published = _iso_to_dt(item.published_at)
            if published is not None and published < cutoff:
                continue
            if query and query.lower() not in (item.title + item.summary).lower():
                continue
            seen.setdefault(item.id, item)

    items = sorted(
        seen.values(),
        key=lambda it: (
            _iso_to_dt(it.published_at) is None,  # 无时间的排后面
            -(_iso_to_dt(it.published_at) or datetime.now(timezone.utc)).timestamp(),
            -(it.score or 0),
        ),
    )[:limit]
    return {
        "items": [it.to_dict() for it in items],
        "errors": errors,
        "count": len(items),
        "sources": selected,
    }
