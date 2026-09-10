"""极简 MCP Server：stdio + 换行分隔 JSON-RPC 2.0。

刻意只实现必要部分（``initialize`` / ``tools/list`` / ``tools/call`` /
``ping`` + 透传通知），且**零依赖**（stdlib only）——STEPWORK 的
``McpStdioClient`` 就是「一行一个 JSON、不带 Content-Length」，所以这里
也不带。能少一个依赖就少一个「装不上」的理由。
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .douhot import iter_boards
from .models import SourceError
from .sources import SOURCES, discover

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "stepwork-hotspot-mcp", "version": "0.1.0"}

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_sources",
        "description": "列出可用热点源（形态、是否需密钥、实测备注）",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "discover_hotspots",
        "description": (
            "抓取热点条目：按时间窗过滤、跨源去重、按时间/热度排序。"
            "某个源失败不会让整个调用失败，但会列在 errors 里。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sources": {
                    "type": "array",
                    "items": {"type": "string", "enum": sorted(SOURCES)},
                    "description": "不传 = 全部源",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "windowHours": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "只看最近 N 小时；无时间的条目不过滤",
                    "default": 48,
                },
                "query": {"type": "string", "description": "标题/摘要包含该词（不分大小写）"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_douhot_boards",
        "description": (
            "列出抖音热点宝可抓的榜单（需已用 --remote-debugging-port 启动浏览器并登录）。"
            "用于让上层选榜；失败会在 error 里说清是没装 playwright 还是没连上/没登录。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _ok(result: Any, msg_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _err(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _call_tool(name: str, args: dict[str, Any]) -> Any:
    if name == "list_sources":
        return {
            "sources": [
                {
                    "id": s.id,
                    "title": s.title,
                    "kind": s.kind,
                    "needsKey": s.needs_key,
                    "requiresLogin": s.requires_login,
                    "note": s.note,
                }
                for s in SOURCES.values()
            ]
        }
    if name == "discover_hotspots":
        try:
            return discover(
                sources=args.get("sources"),
                limit=int(args.get("limit") or 20),
                window_hours=int(args.get("windowHours") or 48),
                query=args.get("query"),
            )
        except SourceError as e:
            raise ValueError(str(e)) from None
    if name == "list_douhot_boards":
        try:
            return {"boards": list(iter_boards())}
        except SourceError as e:
            raise ValueError(str(e)) from None
        except Exception as e:  # noqa: BLE001 - CDP 异常统一转成可读错误
            raise ValueError(f"{type(e).__name__}: {e}") from None
    raise ValueError(f"unknown tool: {name}")


def handle(req: dict[str, Any]) -> dict[str, Any] | None:
    """处理一条请求；通知类返回 ``None``（不应回复）。"""
    method = req.get("method")
    msg_id = req.get("id")

    if method == "initialize":
        return _ok(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            },
            msg_id,
        )
    if method == "ping":
        return _ok({}, msg_id)
    if method == "tools/list":
        return _ok({"tools": _TOOLS}, msg_id)
    if method == "tools/call":
        params = req.get("params") or {}
        try:
            payload = _call_tool(params.get("name", ""), params.get("arguments") or {})
        except ValueError as e:
            return _err(msg_id, -32602, str(e))
        # MCP 文本内容统一包一层，避免不同客户端对 result 形状各说各话
        return _ok(
            {
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False)}
                ],
                "isError": False,
            },
            msg_id,
        )
    if isinstance(method, str) and method.startswith("notifications/"):
        return None
    if msg_id is None:
        return None
    return _err(msg_id, -32601, f"method not found: {method}")


def serve(stdin: TextIO, stdout: TextIO) -> None:
    """主循环：一行一请求，遇到 EOF 退出。"""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps(_err(None, -32700, "parse error")) + "\n")
            stdout.flush()
            continue
        resp = handle(req)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> None:
    serve(sys.stdin, sys.stdout)


if __name__ == "__main__":
    main()
