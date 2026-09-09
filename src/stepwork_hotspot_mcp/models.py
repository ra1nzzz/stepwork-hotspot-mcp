"""热点条目的统一形状。

为什么先定这个：连接器的形态差异极大（JSON API / RSS / 抓 HTML），
下游（STEPWORK 的 ``GenerateTopic``）只吃文本。统一成一行一条
``title + summary + url``，才能让「换源」不影响下游。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class HotspotItem:
    """一个热点条目。

    Attributes:
        source: 来源 id（如 ``huggingface_daily``）。
        title: 标题（唯一必填）。
        url: 原文链接；没有则空串（不要塞 None 进 JSON）。
        summary: 摘要/正文片段，已去标签、压空白。
        published_at: ISO 8601 字符串；源没给就是 ``None``。
        score: 热度（点赞/星数/评论数），源没给就是 ``None``。
        meta: 源特有字段（作者、语言、仓库全名等），不进主流程。
    """

    source: str
    title: str
    url: str = ""
    summary: str = ""
    published_at: str | None = None
    score: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """稳定 id：同（源, 标题, 链接）永远同 id，便于下游去重与缓存。"""
        seed = f"{self.source}\x00{self.title}\x00{self.url}".encode()
        return hashlib.sha256(seed).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "summary": self.summary,
            "publishedAt": self.published_at,
            "score": self.score,
            "meta": self.meta,
        }


class SourceError(RuntimeError):
    """某个源抓取失败。

    继承 ``RuntimeError`` 而非自定义 ``Exception``：错误串会被直接转给用户
    （``f"{type(e).__name__}: {e}"``），类名必须人能读懂。
    """
