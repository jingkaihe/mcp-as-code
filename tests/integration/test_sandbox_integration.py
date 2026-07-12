from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
import json
from pathlib import Path
import shutil
import subprocess
import threading
from typing import Any
from urllib.parse import urlsplit

import pytest

from maco.codegen import generate_sandbox_sdk
from maco.sandbox import (
    DEFAULT_SANDBOX_IMAGE,
    DockerSandboxProvider,
    GatewayInfo,
    LocalSandboxProvider,
    MatchlockSandboxProvider,
    SANDBOX_USER,
    SandboxContext,
    SandboxExec,
    default_matchlock_gateway_ip,
)
from maco.serve_mcp import _detect_docker_gateway_ip, _is_docker_desktop


TOKEN = "integration-token"


def test_local_provider_executes_with_gateway_and_generated_workspace(tmp_path):
    with fake_gateway() as gateway:
        context = _context(tmp_path, gateway.url, TOKEN)
        result = _run_smoke(LocalSandboxProvider(context))

    _assert_smoke(result, expected_gateway_url=gateway.url)


def test_docker_provider_executes_with_gateway_and_generated_workspace(tmp_path):
    _require_docker()
    _docker_pull_or_skip(DEFAULT_SANDBOX_IMAGE)
    gateway_ip = _docker_gateway_ip()
    with fake_gateway(host="0.0.0.0") as gateway:
        context = _context(tmp_path, gateway.url, TOKEN)
        provider = DockerSandboxProvider(context, image=DEFAULT_SANDBOX_IMAGE, gateway_ip=gateway_ip)
        try:
            result = _run_smoke(provider)
        finally:
            provider.stop()

    _assert_smoke(result, expected_gateway_url=_guest_url(gateway.url, "host.docker.internal"))


def test_matchlock_provider_executes_with_gateway_and_generated_workspace(tmp_path):
    _require_matchlock()
    with fake_gateway(host="0.0.0.0") as gateway:
        context = _context(tmp_path, gateway.url, TOKEN)
        provider = MatchlockSandboxProvider(
            context,
            image=DEFAULT_SANDBOX_IMAGE,
            gateway_ip=default_matchlock_gateway_ip(),
        )
        try:
            result = _run_smoke(provider)
        finally:
            provider.stop()

    _assert_smoke(result, expected_gateway_url=_guest_url(gateway.url, "maco-gateway.internal"))


def _run_smoke(provider: LocalSandboxProvider | DockerSandboxProvider | MatchlockSandboxProvider):
    script = r'''
import asyncio
import json
import os
import urllib.request

from tools.echo_server import EchoInput, echo

request = urllib.request.Request(
    os.environ["MACO_GATEWAY_URL"] + "health",
    headers={"Authorization": "Bearer " + os.environ["MACO_GATEWAY_TOKEN"]},
)
with urllib.request.urlopen(request, timeout=5) as response:
    health = json.loads(response.read().decode("utf-8"))

async def main():
    tool_result = await echo(EchoInput(message="sandbox-smoke"))
    print(json.dumps({
        "gateway_url": os.environ["MACO_GATEWAY_URL"],
        "identity": f"{os.getuid()}:{os.getgid()}",
        "workspace": os.environ["MACO_WORKSPACE"],
        "tool_result": tool_result.result,
        "health": health,
    }, sort_keys=True))

asyncio.run(main())
'''
    return provider.run(SandboxExec(command=f"python - <<'PY'\n{script}\nPY", timeout=90))


def _assert_smoke(result: Any, *, expected_gateway_url: str) -> None:
    assert result.exit_code == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, result.stdout
    payload = json.loads(lines[-1])
    assert payload["gateway_url"] == expected_gateway_url
    if not payload["workspace"].endswith(".maco"):
        assert payload["identity"] == SANDBOX_USER
    assert payload["workspace"].endswith("macosdk") or payload["workspace"].endswith(".maco")
    assert payload["tool_result"] == "sandbox-smoke"
    assert payload["health"] == {"ok": True}


def _context(tmp_path: Path, gateway_url: str, token: str) -> SandboxContext:
    workspace = tmp_path / ".maco"
    generate_sandbox_sdk(_echo_catalog(), workspace=workspace)
    (workspace / "gateway.json").write_text(
        json.dumps({"url": gateway_url, "token": token}),
        encoding="utf-8",
    )
    return SandboxContext(
        workspace=workspace.resolve(),
        scratch=(tmp_path / "scratch").resolve(),
        gateway=GatewayInfo.from_file(workspace / "gateway.json"),
        timeout=90,
    )


class fake_gateway:
    def __init__(self, host: str = "127.0.0.1") -> None:
        self._bind_host = host
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def __enter__(self) -> fake_gateway:
        httpd = ThreadingHTTPServer((self._bind_host, 0), _GatewayHandler)
        actual_host, actual_port = httpd.server_address[:2]
        display_host = "127.0.0.1" if actual_host in {"0.0.0.0", ""} else actual_host
        self.url = f"http://{display_host}:{actual_port}/"
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self._httpd = httpd
        self._thread = thread
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        assert self._httpd is not None
        assert self._thread is not None
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


class _GatewayHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.end_headers()
            return
        if self.path.rstrip("/") in {"", "/health"}:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
            return
        if self.path.rstrip("/") == "/tools":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps({"servers": _echo_catalog()}).encode("utf-8")
            )
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or "0")
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps({"structuredContent": {"result": request.get("arguments", {}).get("message")}}).encode(
                "utf-8"
            )
        )

    def log_message(self, format: str, *args: Any) -> None:
        return


def _echo_catalog() -> dict[str, list[dict[str, Any]]]:
    return {
        "echo-server": [
            {
                "name": "echo",
                "description": "Echo a message",
                "inputSchema": {
                    "type": "object",
                    "required": ["message"],
                    "properties": {"message": {"type": "string"}},
                },
                "outputSchema": {
                    "type": "object",
                    "required": ["result"],
                    "properties": {"result": {"type": "string"}},
                },
            }
        ]
    }


def _guest_url(url: str, host: str) -> str:
    parts = urlsplit(url)
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return parts._replace(netloc=netloc).geturl()


def _require_docker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker binary not available")
    result = subprocess.run(
        ["docker", "info"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"docker daemon not available: {result.stderr.strip()}")


def _docker_pull_or_skip(image: str) -> None:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    if result.returncode == 0:
        return
    pull = subprocess.run(
        ["docker", "pull", image],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
        check=False,
    )
    if pull.returncode != 0:
        pytest.skip(f"could not pull {image}: {pull.stderr.strip()}")


def _docker_gateway_ip() -> str | None:
    if _is_docker_desktop("docker"):
        return None
    gateway_ip = _detect_docker_gateway_ip("docker", None)
    if gateway_ip is None:
        pytest.skip("could not detect Docker bridge gateway IP")
    return gateway_ip


def _require_matchlock() -> None:
    if find_spec("matchlock") is None:
        pytest.skip("Matchlock Python SDK not installed; install mcp-as-code[matchlock]")
    if shutil.which("matchlock") is None:
        pytest.skip("matchlock binary not available")
    result = subprocess.run(
        ["matchlock", "diagnose", "--json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"matchlock diagnose failed: {result.stderr.strip()}")
