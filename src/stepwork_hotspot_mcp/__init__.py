"""stepwork-hotspot-mcp：STEPWORK 的上游热点发现 MCP Server。

独立仓库、独立安装、零依赖（stdlib only）。STEPWORK 侧通过既有的
``AddMcpServer`` / ``CallMcpTool`` 接入，本服务对 STEPWORK 一无所知。
"""

from .models import HotspotItem, SourceError
from .sources import SOURCES, discover

__all__ = ["HotspotItem", "SourceError", "SOURCES", "discover"]
__version__ = "0.1.0"
