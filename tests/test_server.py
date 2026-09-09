"""MCP 协议层测试（不启子进程，直接喂 stdin/stdout 替身）。

锁死两件事：① STEPWORK 的 ``McpStdioClient`` 期望「一行一个 JSON、
不带 Content-Length」；② 通知类消息**不能**回复（回了对端会当成响应错配）。
"""
from __future__ import annotations

import io
import json

import pytest

from stepwork_hotspot_mcp.server import handle, serve


def test_initialize_returns_protocol_version_and_tools_capability() -> None:
    resp = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp is not None
    result = resp["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "stepwork-hotspot-mcp"
    assert "tools" in result["capabilities"]


def test_tools_list_exposes_discover_hotspots() -> None:
    resp = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert resp is not None
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "discover_hotspots" in names
    assert "list_sources" in names


def test_tools_call_list_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_sources", "arguments": {}},
        }
    )
    assert resp is not None
    text = resp["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert {s["id"] for s in payload["sources"]} >= {
        "huggingface_daily",
        "github_trending",
        "rss",
    }


def test_tools_call_wraps_payload_as_text_content(monkeypatch: pytest.MonkeyPatch) -> None:
    import stepwork_hotspot_mcp.server as srv

    monkeypatch.setattr(srv, "discover", lambda **kwargs: {"items": [], "count": 0})
    resp = handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "discover_hotspots",
                "arguments": {"limit": 5, "windowHours": 24},
            },
        }
    )
    assert resp is not None
    assert resp["result"]["isError"] is False
    assert json.loads(resp["result"]["content"][0]["text"])["count"] == 0


def test_notifications_get_no_reply() -> None:
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_an_error() -> None:
    resp = handle({"jsonrpc": "2.0", "id": 9, "method": "nope"})
    assert resp is not None
    assert resp["error"]["code"] == -32601


def test_serve_roundtrip_and_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    import stepwork_hotspot_mcp.server as srv

    monkeypatch.setattr(srv, "discover", lambda **kwargs: {"items": [], "count": 0})
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        + "\n{not json}\n"
    )
    stdout = io.StringIO()
    serve(stdin, stdout)
    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == 1
    assert json.loads(lines[1])["error"]["code"] == -32700
