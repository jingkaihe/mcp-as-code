from __future__ import annotations

import asyncio
import json
from pathlib import Path
import re
import subprocess

import pytest
from starlette.testclient import TestClient

from maco.sandbox import GatewayInfo, SandboxContext, SandboxExec, SandboxRunResult
import maco.serve_mcp as serve_mcp_module
from maco.serve_mcp import (
    _bash_description,
    _clean_local_sdk,
    _code_execute_description,
    _content_addressed_script_filename,
    _default_gateway_host,
    _detect_docker_gateway_ip,
    _docker_gateway_ip,
    _gateway_extra_hosts,
    _is_docker_desktop,
    _mcp_instructions,
    _result_payload,
    _validate_managed_gateway_bind,
    create_serve_mcp_app,
    default_scratch_path,
)


def test_serve_mcp_code_execute_uses_provider_script_command(tmp_path):
    context = _context(tmp_path)
    provider = RecordingProvider()
    app = create_serve_mcp_app(provider, context)

    result = app.call_tool("code_execute", {"code": "print('hello')", "filename": "task.py"})
    if hasattr(result, "__await__"):
        asyncio.run(result)

    assert provider.writes == [("task.py", "print('hello')")]
    assert provider.requests[0].command == "python /workspace/task.py"


def test_default_scratch_path_lives_under_workspace(tmp_path):
    workspace = tmp_path / ".maco"

    assert default_scratch_path(workspace) == workspace / "scratch"


def test_clean_local_sdk_removes_only_generated_sdk_artifacts(tmp_path):
    workspace = tmp_path / ".maco"
    (workspace / "tools" / "stale_server").mkdir(parents=True)
    (workspace / "tools" / "stale_server" / "stale.py").write_text("", encoding="utf-8")
    (workspace / "manifest.json").write_text("{}", encoding="utf-8")
    (workspace / "pyproject.toml").write_text("[project]\nname = 'stale'\n", encoding="utf-8")
    (workspace / "gateway.json").write_text("{}", encoding="utf-8")
    (workspace / "scratch").mkdir()
    (workspace / "scratch" / "task.py").write_text("print('keep')\n", encoding="utf-8")

    _clean_local_sdk(workspace)

    assert not (workspace / "tools").exists()
    assert not (workspace / "manifest.json").exists()
    assert not (workspace / "pyproject.toml").exists()
    assert (workspace / "gateway.json").exists()
    assert (workspace / "scratch" / "task.py").exists()


def test_serve_mcp_code_execute_omitted_filename_uses_deterministic_path(tmp_path):
    context = _context(tmp_path)
    provider = RecordingProvider()
    app = create_serve_mcp_app(provider, context)
    code = "print('hello')"

    result = app.call_tool("code_execute", {"code": code})
    if hasattr(result, "__await__"):
        asyncio.run(result)

    relative = _content_addressed_script_filename(code)
    assert re.fullmatch(r"[0-9a-f]{16}\.py", relative)
    assert provider.writes == [(relative, code)]
    assert provider.requests[0].command == f"python /workspace/{relative}"


def test_result_payload_contains_only_tool_output_fields():
    payload = _result_payload(SandboxRunResult(0, "out", ""))

    assert payload == {"ok": True, "exit_code": 0, "stdout": "out", "stderr": ""}


def test_serve_mcp_identity_route_returns_detached_identity(tmp_path):
    context = _context(tmp_path)
    provider = RecordingProvider()
    app = create_serve_mcp_app(
        provider,
        context,
        detached_service_id="project-123",
        detached_service_token="identity-token",
    )

    with TestClient(app.streamable_http_app()) as client:
        response = client.get("/_maco/identity")

    assert response.status_code == 200
    assert response.json() == {"id": "project-123", "identity_token": "identity-token"}


def test_serve_mcp_instructions_list_server_modules_and_rg_fd_discovery(tmp_path):
    context = _context(tmp_path)
    provider = RecordingProvider()
    (context.workspace / "tools" / "echo_server").mkdir(parents=True)
    (context.workspace / "tools" / "echo_server" / "__init__.py").write_text(
        "from .echo import echo\n",
        encoding="utf-8",
    )

    instructions = _mcp_instructions(provider, context)

    assert "Available generated server modules:" in instructions
    assert "- echo_server: /workspace/macosdk/tools/echo_server" in instructions
    assert "rg --files /workspace/macosdk/tools" in instructions
    assert "fd . /workspace/macosdk/tools -t f" in instructions
    assert "rg \"^def \" /workspace/macosdk/tools/<server>" in instructions
    assert "MACO_GATEWAY_URL" not in instructions
    assert "PYTHONPATH" not in instructions


def test_serve_mcp_instructions_include_all_server_modules(tmp_path):
    context = _context(tmp_path)
    provider = RecordingProvider()
    for index in range(55):
        server_dir = context.workspace / "tools" / f"server{index:02d}"
        server_dir.mkdir(parents=True)
        (server_dir / "__init__.py").write_text("", encoding="utf-8")

    instructions = _mcp_instructions(provider, context)

    assert "..." not in instructions
    assert "- server00: /workspace/macosdk/tools/server00" in instructions
    assert "- server54: /workspace/macosdk/tools/server54" in instructions


def test_code_execute_description_lists_server_modules(tmp_path):
    context = _context(tmp_path)
    provider = RecordingProvider()
    (context.workspace / "tools" / "github").mkdir(parents=True)
    (context.workspace / "tools" / "github" / "__init__.py").write_text(
        "from .search import search\n",
        encoding="utf-8",
    )

    description = _code_execute_description(provider, context)

    assert "from tools.<server> import <ToolInput>, <tool>" in description
    assert "For most tasks, pass only the code argument" in description
    assert "<hash>.py" in description
    assert "- github: /workspace/macosdk/tools/github" in description
    assert "/workspace/macosdk/tools/<server>/__init__.py" in description
    assert "MACO_GATEWAY_URL" not in description
    assert "PYTHONPATH" not in description


def test_code_execute_schema_describes_optional_arguments(tmp_path):
    context = _context(tmp_path)
    provider = RecordingProvider()
    app = create_serve_mcp_app(provider, context)
    tools = asyncio.run(app.list_tools())
    code_execute = next(tool for tool in tools if tool.name == "code_execute")

    schema = code_execute.inputSchema
    assert schema["required"] == ["code"]
    assert "Import generated tools" in schema["properties"]["code"]["description"]
    assert "<hash>.py" in schema["properties"]["filename"]["description"]
    assert "sys.argv[1:]" in schema["properties"]["args"]["description"]
    assert "server default" in schema["properties"]["timeout"]["description"]


def test_bash_description_uses_concrete_wrapper_paths_without_gateway_details(tmp_path):
    context = _context(tmp_path)
    provider = RecordingProvider()

    description = _bash_description(provider, context)

    assert "rg --files /workspace/macosdk/tools" in description
    assert "fd . /workspace/macosdk/tools -t f" in description
    assert "rg \"^def \" /workspace/macosdk/tools/<server>" in description
    assert "MACO_GATEWAY_URL" not in description
    assert "PYTHONPATH" not in description


def test_matchlock_managed_gateway_defaults_to_linux_tap_gateway(monkeypatch):
    monkeypatch.setattr(serve_mcp_module.sys, "platform", "linux")

    assert _default_gateway_host() == "127.0.0.1"
    gateway_ip = serve_mcp_module.default_matchlock_gateway_ip()

    assert gateway_ip == "192.168.100.1"
    assert _gateway_extra_hosts(
        "matchlock",
        docker_gateway_ip=None,
        matchlock_gateway_ip=gateway_ip,
        explicit_gateway_host=None,
    ) == ("192.168.100.1",)


def test_matchlock_managed_gateway_defaults_to_macos_nat_gateway(monkeypatch):
    monkeypatch.setattr(serve_mcp_module.sys, "platform", "darwin")

    gateway_ip = serve_mcp_module.default_matchlock_gateway_ip()

    assert gateway_ip == "192.168.64.1"


def test_matchlock_managed_gateway_requires_explicit_gateway_host_without_freebind(monkeypatch):
    monkeypatch.setattr(serve_mcp_module, "_supports_freebind", lambda: False)

    with pytest.raises(ValueError, match="--gateway-host 0.0.0.0"):
        _validate_managed_gateway_bind(
            "matchlock",
            explicit_gateway_host=None,
            matchlock_gateway_ip="192.168.100.1",
        )

    _validate_managed_gateway_bind(
        "matchlock",
        explicit_gateway_host="0.0.0.0",
        matchlock_gateway_ip="192.168.100.1",
    )


def test_matchlock_managed_gateway_can_use_default_bind_with_freebind(monkeypatch):
    monkeypatch.setattr(serve_mcp_module, "_supports_freebind", lambda: True)

    _validate_managed_gateway_bind(
        "matchlock",
        explicit_gateway_host=None,
        matchlock_gateway_ip="192.168.100.1",
    )


def test_docker_managed_gateway_defaults_to_local_bind_plus_bridge_extra_host(monkeypatch):
    monkeypatch.setattr(serve_mcp_module, "_is_docker_desktop", lambda _binary: False)
    monkeypatch.setattr(serve_mcp_module, "_detect_docker_gateway_ip", lambda _binary, _network: "172.18.0.1")

    gateway_ip = _docker_gateway_ip(
        None,
        docker_binary="docker-test",
        docker_network="test-network",
    )

    assert _default_gateway_host() == "127.0.0.1"
    assert gateway_ip == "172.18.0.1"
    assert _gateway_extra_hosts(
        "docker",
        docker_gateway_ip=gateway_ip,
        matchlock_gateway_ip=None,
        explicit_gateway_host=None,
    ) == ("172.18.0.1",)


def test_docker_managed_gateway_preserves_docker_desktop_alias(monkeypatch):
    monkeypatch.setattr(serve_mcp_module, "_is_docker_desktop", lambda _binary: True)
    monkeypatch.setattr(
        serve_mcp_module,
        "_detect_docker_gateway_ip",
        lambda _binary, _network: pytest.fail("should not inspect docker networks on Docker Desktop"),
    )

    gateway_ip = _docker_gateway_ip(
        None,
        docker_binary="docker-test",
        docker_network=None,
    )

    assert gateway_ip is None
    assert _gateway_extra_hosts(
        "docker",
        docker_gateway_ip=gateway_ip,
        matchlock_gateway_ip=None,
        explicit_gateway_host=None,
    ) == ()


def test_docker_managed_gateway_requires_detected_native_linux_gateway(monkeypatch):
    monkeypatch.setattr(serve_mcp_module, "_is_docker_desktop", lambda _binary: False)
    monkeypatch.setattr(serve_mcp_module, "_detect_docker_gateway_ip", lambda _binary, _network: None)

    with pytest.raises(ValueError, match="pass --docker-gateway-ip"):
        _docker_gateway_ip(
            None,
            docker_binary="docker-test",
            docker_network="custom-net",
        )


def test_is_docker_desktop_uses_docker_operating_system(monkeypatch):
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ["docker-test", "info", "--format", "{{.OperatingSystem}}"]
        return subprocess.CompletedProcess(command, 0, stdout="Docker Desktop\n", stderr="")

    monkeypatch.setattr(serve_mcp_module.sys, "platform", "linux")
    monkeypatch.setattr(serve_mcp_module.subprocess, "run", fake_run)

    assert _is_docker_desktop("docker-test") is True


def test_detect_docker_gateway_ip_reads_network_gateway(monkeypatch):
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ["docker-test", "network", "inspect", "custom-net"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps([{"IPAM": {"Config": [{"Gateway": "172.19.0.1"}]}}]),
            stderr="",
        )

    monkeypatch.setattr(serve_mcp_module.subprocess, "run", fake_run)

    assert _detect_docker_gateway_ip("docker-test", "custom-net") == "172.19.0.1"


def _context(tmp_path: Path) -> SandboxContext:
    workspace = tmp_path / ".maco"
    (workspace / "tools").mkdir(parents=True)
    (workspace / "gateway.json").write_text(
        json.dumps({"url": "http://127.0.0.1:9/", "token": "secret-token"}),
        encoding="utf-8",
    )
    return SandboxContext(
        workspace=workspace.resolve(),
        scratch=(tmp_path / "scratch").resolve(),
        gateway=GatewayInfo.from_file(workspace / "gateway.json"),
    )


class RecordingProvider:
    guest_workspace = "/workspace/macosdk"
    guest_scratch = "/workspace"

    def __init__(self) -> None:
        self.requests: list[SandboxExec] = []
        self.writes: list[tuple[str, str]] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def write_file(self, relative_path: str, content: str) -> str:
        self.writes.append((relative_path, content))
        return f"/workspace/{relative_path}"

    def run(self, request: SandboxExec) -> SandboxRunResult:
        self.requests.append(request)
        return SandboxRunResult(0, "", "")

    def python_script_command(self, guest_script_path: str, args: list[str]) -> str:
        return " ".join(["python", guest_script_path, *args])
