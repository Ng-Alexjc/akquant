"""Read-first Choice 妙想 MCP client with explicit write safeguards."""

from __future__ import annotations

from typing import Any

from .config import MiaoxiangConfig

_TRADE_WRITE_TOKENS = ("trade", "order", "buy", "sell", "交易", "下单", "委托")
_SELF_SELECT_TOKENS = ("self", "select", "watch", "自选")


class MiaoxiangClient:
    def __init__(self, config: MiaoxiangConfig) -> None:
        self.config = config

    @property
    def ready(self) -> bool:
        return bool(self.config.enabled and self.config.url and self.config.em_api_key)

    def _headers(self) -> dict[str, str]:
        """Use the exact header name published by the Choice 妙想 guide."""
        return {"em_api_key": self.config.em_api_key}

    async def list_tools(self) -> list[dict[str, Any]]:
        if not self.ready:
            return []
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(
            headers=self._headers(), timeout=self.config.timeout_seconds
        ) as client:
            async with streamable_http_client(
                self.config.url, http_client=client
            ) as streams:
                # MCP SDK versions yield either (read, write) or
                # (read, write, session_id_callback).  The transport itself
                # is otherwise identical for our read-only calls.
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    response = await session.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": getattr(
                    tool, "inputSchema", getattr(tool, "input_schema", {})
                ),
            }
            for tool in response.tools
        ]

    def authorize_tool(
        self, name: str, *, confirmed_self_select_target: bool = False
    ) -> None:
        lowered = name.lower()
        if any(token in lowered for token in _TRADE_WRITE_TOKENS):
            raise PermissionError("妙想模拟/真实交易写工具已禁用")
        is_self_select = any(token in lowered for token in _SELF_SELECT_TOKENS)
        if is_self_select and not confirmed_self_select_target:
            raise PermissionError("首次写入自选前必须确认目标自选组")
        if is_self_select and not self.config.allow_self_select_management:
            raise PermissionError("自选管理未启用")
        if is_self_select:
            return
        if not self.config.allow_read_tools:
            raise PermissionError("妙想只读工具未启用")
        if name not in self.config.read_tool_allowlist:
            raise PermissionError("工具尚未经过 tools/list 审核并加入只读白名单")

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        confirmed_self_select_target: bool = False,
    ) -> Any:
        self.authorize_tool(
            name, confirmed_self_select_target=confirmed_self_select_target
        )
        if not self.ready:
            raise RuntimeError("妙想 MCP 未配置")
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(
            headers=self._headers(), timeout=self.config.timeout_seconds
        ) as client:
            async with streamable_http_client(
                self.config.url, http_client=client
            ) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool(name, arguments)
