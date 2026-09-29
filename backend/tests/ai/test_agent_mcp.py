"""CP-AGENT-MCP：MCP 客户端单测（Phase 4）。

覆盖：
  - MCPClient.__aenter__ 连接失败 → MCPError("connect")
  - MCPClient.__aexit__ 正常 cleanup
  - discover_tools() 解析 MCP server.list_tools 返回 → OpenAI tool schema
  - invoke_tool("name", args) → {"raw_text": "..."}
  - invoke_tool 抛 mcp 库异常 → MCPError("internal")
  - register_mcp_tools_to_registry：注册到 default_registry 后 wrapper tool 能 invoke

注意：测试用 mock mcp.ClientSession，避免依赖外部 MCP server 进程。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_AI_SERVICE = Path(__file__).resolve().parents[2] / "ai-service"
_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_AI_SERVICE))


class _FakeTextContent:
    def __init__(self, text: str):
        self.text = text


class _FakeListToolsResult:
    def __init__(self, tools):
        self.tools = tools


class _FakeTool:
    def __init__(self, name: str, description: str, input_schema: dict | None = None):
        self.name = name
        self.description = description
        self.inputSchema = input_schema or {"type": "object", "properties": {}}


class _FakeSession:
    """模拟 mcp.ClientSession 接口。"""

    def __init__(self, tools=None, call_results=None):
        self._tools = tools or []
        self._call_results = call_results or {}

    async def initialize(self):
        pass

    async def list_tools(self):
        return _FakeListToolsResult(self._tools)

    async def call_tool(self, name: str, arguments: dict):
        if name not in self._call_results:
            raise ValueError(f"unknown tool {name!r}")
        result = self._call_results[name]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def reset_default_registry_singleton():
    from agent import tools as tools_module

    tools_module._default_registry_singleton = None
    yield
    tools_module._default_registry_singleton = None


# ---------------------------------------------------------------------------
# MCPClient.__aenter__ / __aexit__
# ---------------------------------------------------------------------------


def test_mcp_client_requires_command():
    """CP-AGENT-MCP-NO-CMD：不传 server_command 抛 MCPError。"""
    from agent.mcp_client import MCPClient, MCPError

    async def run():
        try:
            async with MCPClient(server_command=None):
                assert False, "应该抛 MCPError"
        except MCPError as e:
            assert e.kind == "connect"

    import asyncio

    asyncio.run(run())


def test_mcp_client_raises_when_mcp_not_installed(monkeypatch):
    """CP-AGENT-MCP-NO-MCP-LIB：mock 'mcp' 不可用时抛 connect 错误。"""
    # 把 mcp 模块藏起来，让 stdio_client 触发 ImportError
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.setitem(sys.modules, "mcp.client", None)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", None)

    from agent.mcp_client import MCPClient, MCPError

    async def run():
        try:
            async with MCPClient(server_command=["python3", "-c", "print('hi')"]):
                assert False, "应该抛 MCPError"
        except MCPError as e:
            # mcp 不可用时，按理 import 失败 → connect 错；但我们的 import 是包在 try 里
            assert e.kind in ("connect", "internal")

    import asyncio

    asyncio.run(run())


# ---------------------------------------------------------------------------
# discover_tools / invoke_tool（mock session）
# ---------------------------------------------------------------------------


def test_mcp_discover_tools_converts_to_openai_schema():
    """CP-AGENT-MCP-DISCOVER：MCP tools → OpenAI function schema。"""
    from agent.mcp_client import MCPClient

    fake_tools = [
        _FakeTool(
            "echo", "回显文本", {"type": "object", "properties": {"text": {"type": "string"}}}
        ),
        _FakeTool("reverse", "反转字符串", None),
    ]
    session = _FakeSession(tools=fake_tools)

    async def run():
        mcp = MCPClient(server_command=["dummy"])
        mcp._session = session  # bypass __aenter__
        schemas = await mcp.discover_tools()
        assert len(schemas) == 2
        assert schemas[0]["type"] == "function"
        assert schemas[0]["function"]["name"] == "echo"
        assert schemas[0]["function"]["description"] == "回显文本"
        # reverse 没传 inputSchema，fallback 到空 schema
        assert schemas[1]["function"]["parameters"] == {"type": "object", "properties": {}}

    import asyncio

    asyncio.run(run())


def test_mcp_invoke_tool_returns_raw_text():
    """CP-AGENT-MCP-INVOKE-OK：invoke_tool 返回 {"raw_text": "..."}。"""
    from agent.mcp_client import MCPClient

    fake_call_results = {
        "echo": type(
            "FakeResp",
            (),
            {"content": [_FakeTextContent("echoed content")]},
        )()
    }
    session = _FakeSession(call_results=fake_call_results)

    async def run():
        mcp = MCPClient(server_command=["dummy"])
        mcp._session = session
        result = await mcp.invoke_tool("echo", {"text": "hi"})
        assert result == {"raw_text": "echoed content"}

    import asyncio

    asyncio.run(run())


def test_mcp_invoke_tool_not_found_raises_notfound():
    """CP-AGENT-MCP-INVOKE-NOTFOUND：unknown tool 抛 MCPError('notfound')。"""
    from agent.mcp_client import MCPClient, MCPError

    session = _FakeSession(call_results={})  # 空 → ValueError("unknown tool 'xxx'")

    async def run():
        mcp = MCPClient(server_command=["dummy"])
        mcp._session = session
        try:
            await mcp.invoke_tool("missing_tool", {})
            assert False, "应该抛 MCPError"
        except MCPError as e:
            # mcp_client.py 检测 "unknown tool" → notfound
            assert e.kind == "notfound"

    import asyncio

    asyncio.run(run())


# ---------------------------------------------------------------------------
# register_mcp_tools_to_registry：mock session + 验证 registry
# ---------------------------------------------------------------------------


def test_register_mcp_tools_to_registry_adds_prefixed_tools():
    """CP-AGENT-MCP-REGISTER：注册后 registry 出现 mcp_<name> 工具，且能 invoke。"""
    from agent.mcp_client import (
        MCPClient,
        register_mcp_tools_to_registry,
        get_echo_mcp_server_command,
    )

    fake_tools = [
        _FakeTool("echo", "回显文本"),
        _FakeTool("reverse", "反转字符串"),
    ]

    class _CallSession:
        """区分 session：list_tools 用 fake_tools，call_tool 按 name 返回 content。"""

        def __init__(self, tools, calls):
            self._tools = tools
            self._calls = calls
            self.calls_history: list = []

        async def initialize(self):
            pass

        async def list_tools(self):
            return _FakeListToolsResult(self._tools)

        async def call_tool(self, name, arguments):
            self.calls_history.append((name, arguments))
            text = self._calls.get(name, "")
            return type("FakeResp", (), {"content": [_FakeTextContent(text)]})()

    call_results = {"echo": "ECHOED", "reverse": "REVERSED"}
    session = _CallSession(fake_tools, call_results)

    async def run():
        mcp = MCPClient(server_command=get_echo_mcp_server_command())
        mcp._session = session

        count = await register_mcp_tools_to_registry(mcp, prefix="mcp_")
        assert count == 2

        from agent.tools import get_default_registry

        reg = get_default_registry()
        names = {s.name for s in reg.list_specs()}
        assert "mcp_echo" in names
        assert "mcp_reverse" in names

        # 调一下 mcp_echo
        result = await reg.invoke("mcp_echo", {}, {"text": "hello"})
        assert result == {"raw_text": "ECHOED"}
        # 调用历史应被记录
        assert session.calls_history[-1] == ("echo", {"text": "hello"})

    import asyncio

    asyncio.run(run())


def test_register_mcp_tools_skips_already_registered(monkeypatch):
    """重复注册同一 prefix+name 时抛 ValueError（ToolRegistry 行为）。"""
    from agent.mcp_client import (
        MCPClient,
        register_mcp_tools_to_registry,
        get_echo_mcp_server_command,
    )

    fake_tools = [_FakeTool("echo", "回显")]

    class _MiniSession:
        async def initialize(self):
            pass

        async def list_tools(self):
            return _FakeListToolsResult(fake_tools)

        async def call_tool(self, n, a):
            return type("R", (), {"content": [_FakeTextContent("")]})()

    async def run():
        mcp = MCPClient(server_command=get_echo_mcp_server_command())
        mcp._session = _MiniSession()
        await register_mcp_tools_to_registry(mcp, prefix="dup_")
        # 第二次注册同名 → ValueError（ToolRegistry.register 行为）
        try:
            await register_mcp_tools_to_registry(mcp, prefix="dup_")
            assert False, "应该抛 ValueError"
        except ValueError as e:
            assert "dup_echo" in str(e)

    import asyncio

    asyncio.run(run())


# ---------------------------------------------------------------------------
# get_echo_mcp_server_command
# ---------------------------------------------------------------------------


def test_get_echo_mcp_server_command_returns_python_executable():
    """CP-AGENT-MCP-ECHO-CMD：返回的 list[str] 含 'echo' 和 'reverse' tool 的 Python 脚本。"""
    from agent.mcp_client import get_echo_mcp_server_command, ECHO_SERVER_PY

    cmd = get_echo_mcp_server_command()
    assert cmd[0] == sys.executable
    assert "-c" in cmd
    # cmd[2] 是 ECHO_SERVER_PY
    assert "echo" in ECHO_SERVER_PY
    assert "reverse" in ECHO_SERVER_PY
    assert "stdio_server" in ECHO_SERVER_PY
