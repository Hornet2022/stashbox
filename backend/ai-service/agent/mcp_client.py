"""CP-AGENT-MCP：听匣 agent 的 MCP 客户端。

MCP（Model Context Protocol）是 Anthropic 提出的"工具调用协议"。agent 通过
MCP 客户端连接外部 MCP server，能动态发现 + 调用 server 暴露的工具。

Phase 4 实现：
  - MCPClient（async context manager）：通过 stdio_client + ClientSession 连接
  - discover_tools() → 列出 server 暴露的所有 tool
  - invoke_tool(name, args) → 调用单个 tool
  - 把 MCP server 的 tool 包装成 ToolSpec 注册到 default_registry

Phase 4 留 todo：把 MCP server 真正接入（需要 1 个 MCP server 进程或 SSE endpoint）。

为什么 MCP 跟 Phase 1/2 区别：
  Phase 2 tool registry：tool 写在 Python 代码里，跟 agent 同进程。
  Phase 4 MCP：tool 写在独立进程 / 独立服务，agent 通过 MCP 协议发现 + 调用。
  好处：tool 可以独立部署（fetch_url MCP server 单独进程，跟 agent 解耦）。

设计原则：
  - MCPClient 是 async context manager —— enter 时 connect，exit 时 disconnect
  - invoke_tool 返回 dict（与 tool spec schema 一致）
  - 不缓存 session —— 每次 invoke 都重新连，避免 stale connection
"""

from __future__ import annotations

import logging
import sys
from typing import Any

log = logging.getLogger("agent.mcp")


class MCPError(Exception):
    """MCP 客户端异常（连接失败 / tool 调用失败）。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # connect / timeout / notfound / internal
        self.message = message


class MCPClient:
    """CP-AGENT-MCP-CLIENT：MCP server 连接 + tool 发现 + tool 调用。

    Phase 4 用 stdio transport（subprocess + stdin/stdout），未来支持 SSE / HTTP。

    用法：
      async with MCPClient(server_command=["python", "mcp_server.py"]) as mcp:
          tools = await mcp.discover_tools()
          result = await mcp.invoke_tool("fetch_url", {"url": "..."})
    """

    def __init__(self, server_command: list[str] | None = None) -> None:
        self._server_command = server_command
        self._session = None
        self._read = None
        self._write = None
        self._exit_stack: Any = None

    async def __aenter__(self) -> "MCPClient":
        if not self._server_command:
            # Phase 4 占位：未来支持 SSE / HTTP transport
            raise MCPError("connect", "未配置 server_command（Phase 4 仅支持 stdio）")

        try:
            # mcp 1.x 库：stdio_client + ClientSession
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            import contextlib

            self._exit_stack = contextlib.AsyncExitStack()
            params = StdioServerParameters(
                command=self._server_command[0],
                args=self._server_command[1:],
            )
            read, write = await self._exit_stack.enter_async_context(stdio_client(params))
            session = await self._exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._session = session
            self._read = read
            self._write = write
            log.info("mcp_client_connected server=%s", self._server_command[0])
            return self
        except ImportError as exc:
            raise MCPError(
                "connect",
                f"mcp 库未安装：{exc}",
            ) from exc
        except Exception as exc:
            raise MCPError("connect", f"MCP 连接失败: {exc}") from exc

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception as exc:
                log.warning(f"mcp_client_disconnect_error error={exc!s}")
            self._exit_stack = None
        self._session = None

    async def discover_tools(self) -> list[dict[str, Any]]:
        """CP-AGENT-MCP-DISCOVER：列出 MCP server 暴露的所有 tool schema。

        返回 list of OpenAI-format tool schema：
            [{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}]
        """
        if self._session is None:
            raise MCPError("connect", "MCPClient 未连接")
        try:
            result = await self._session.list_tools()
        except Exception as exc:
            raise MCPError("internal", f"list_tools 失败: {exc}") from exc

        tools: list[dict[str, Any]] = []
        for tool in result.tools:
            # CP-AGENT-MCP-COMPAT：mcp 1.x 用 inputSchema，2.x 用 input_schema。
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": getattr(tool, "description", "") or "",
                        "parameters": schema
                        or {
                            "type": "object",
                            "properties": {},
                        },
                    },
                }
            )
        log.info("mcp_discover_tools count=%d", len(tools))
        return tools

    async def invoke_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """CP-AGENT-MCP-INVOKE：调用 MCP server 上的 tool。

        返回 dict（tool 自己的 result payload）。
        """
        if self._session is None:
            raise MCPError("connect", "MCPClient 未连接")
        try:
            result = await self._session.call_tool(name, arguments)
        except Exception as exc:
            # mcp 库：tool not found 会抛特定异常。简化：归 internal
            err_str = str(exc)
            if "not found" in err_str.lower() or "unknown tool" in err_str.lower():
                raise MCPError("notfound", f"tool {name!r} 未找到: {exc}") from exc
            raise MCPError("internal", f"invoke_tool 失败: {exc}") from exc

        # result.content 是 list[TextContent | ImageContent | ...]，统一取 text
        if hasattr(result, "content") and result.content:
            text_parts: list[str] = []
            for part in result.content:
                if hasattr(part, "text"):
                    text_parts.append(part.text)
            if text_parts:
                # Phase 4 简化：返回 {"raw_text": "..."}，与 ToolRegistry 期望 dict 对齐
                return {"raw_text": "\n".join(text_parts)}
        return {"raw": str(result)}


# ---------------------------------------------------------------------------
# 注册 MCP 工具到 default_registry（Phase 4：动态发现 server tools）
# ---------------------------------------------------------------------------


async def register_mcp_tools_to_registry(mcp_client: MCPClient, prefix: str = "mcp_") -> int:
    """CP-AGENT-MCP-REGISTER：把 MCP server 暴露的 tool 注册到 default_registry。

    每个 tool 名加 prefix（默认 "mcp_"）避免和 Python 内置 tool 冲突。
    返回注册的 tool 数量。
    """
    from .tools import ToolSpec, get_default_registry

    schemas = await mcp_client.discover_tools()
    registry = get_default_registry()
    count = 0
    for schema in schemas:
        tool_name = f"{prefix}{schema['function']['name']}"

        async def _invoker(
            state: dict[str, Any],
            args: dict[str, Any],
            *,
            _client=mcp_client,
            _name=schema["function"]["name"],
        ) -> dict[str, Any]:
            try:
                return await _client.invoke_tool(_name, args)
            except MCPError:
                raise  # 让 ToolRegistry 包成 ToolError

        registry.register(
            ToolSpec(
                name=tool_name,
                description=schema["function"]["description"],
                func=_invoker,
            )
        )
        count += 1
    log.info("mcp_registered_tools count=%d prefix=%s", count, prefix)
    return count


# ---------------------------------------------------------------------------
# 1 个示例 MCP server（Phase 4 自带）：echo MCP server
# ---------------------------------------------------------------------------


ECHO_SERVER_PY = '''#!/usr/bin/env python3
"""CP-AGENT-MCP-ECHO-SERVER：Phase 4 自带的最小 MCP server 示例（mcp 2.x API）。

通过 stdio transport 暴露 2 个 tool：
  - echo(text: str)    -> 原样回显
  - reverse(text: str) -> 反转字符串

mcp 2.x 与 1.x 的差异：Server 在 `mcp.server` 下（非顶层），
list_tools / call_tool 通过构造函数回调（on_list_tools / on_call_tool），
而不是 @server.list_tools() 装饰器。
"""
import asyncio

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "输入文本"}},
    "required": ["text"],
}


async def on_list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="echo",
                description="回显输入文本（CP-AGENT-MCP 示例）",
                input_schema=_TEXT_SCHEMA,
            ),
            types.Tool(
                name="reverse",
                description="反转输入字符串",
                input_schema=_TEXT_SCHEMA,
            ),
        ]
    )


async def on_call_tool(ctx, params):
    name = params.name
    args = params.arguments or {}
    text = str(args.get("text", ""))
    if name == "echo":
        out = text
    elif name == "reverse":
        out = text[::-1]
    else:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"unknown tool: {name}")],
            is_error=True,
        )
    return types.CallToolResult(content=[types.TextContent(type="text", text=out)])


async def main() -> None:
    server = Server(
        "echo-mcp-server",
        version="0.1.0",
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
'''


def get_echo_mcp_server_command() -> list[str]:
    """CP-AGENT-MCP-ECHO-CMD：echo server 的 stdio 启动命令。

    生产用法：list[str] 是 ['python3', '-c', ECHO_SERVER_PY]。
    测试用法：可 mock 掉。
    """
    return [sys.executable, "-c", ECHO_SERVER_PY]
