from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

import maco
from maco.sandbox import (
    DEFAULT_SANDBOX_IMAGE,
    DockerSandboxProvider,
    GatewayInfo,
    LocalSandboxProvider,
    MatchlockSandboxProvider,
    SANDBOX_USER,
    SandboxContext,
    SandboxError,
    SandboxExec,
    SandboxRunResult,
    guest_path_for,
    translate_loopback_url,
    write_code_file,
)
import maco.sandbox.core as sandbox_core
import maco.sandbox.providers.docker as docker_provider
import maco.sandbox.providers.matchlock as matchlock_provider


def test_translate_loopback_url_for_guest_hosts():
    assert (
        translate_loopback_url("http://127.0.0.1:4789/", "host.docker.internal")
        == "http://host.docker.internal:4789/"
    )
    assert (
        translate_loopback_url("http://localhost:4789/path?x=1", "maco-gateway.internal")
        == "http://maco-gateway.internal:4789/path?x=1"
    )
    assert translate_loopback_url("http://gateway.example:4789/", "ignored") == "http://gateway.example:4789/"


def test_local_provider_injects_gateway_and_pythonpath(tmp_path):
    context = _context(tmp_path)
    provider = LocalSandboxProvider(context)

    result = provider.run(
        SandboxExec(
            command="python - <<'PY'\nimport os\nprint(os.environ['MACO_GATEWAY_URL'])\nprint(os.environ['MACO_GATEWAY_TOKEN'])\nprint(os.environ['MACO_WORKSPACE'])\nPY"
        )
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.splitlines() == [
        "http://127.0.0.1:9/",
        "secret-token",
        str(context.workspace),
    ]


def test_docker_provider_bootstraps_and_execs_without_host_sdk_mounts(tmp_path, monkeypatch):
    context = _context(tmp_path)
    provider = DockerSandboxProvider(
        context,
        image="maco-test:latest",
        docker_binary="docker-test",
        gateway_host="host.docker.internal",
        gateway_ip="172.17.0.1",
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[:3] == ["docker-test", "run", "-d"]:
            return subprocess.CompletedProcess(command, 0, stdout="container-123\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(docker_provider.subprocess, "run", fake_run)

    result = provider.run(SandboxExec(command="echo hi", timeout=7))

    assert result.ok
    run_command, run_kwargs = calls[0]
    assert run_command[:3] == ["docker-test", "run", "-d"]
    assert "--rm" in run_command
    assert _flag_value(run_command, "--name").startswith("maco-sandbox-")
    assert "maco.managed=true" in run_command
    assert _flag_value(run_command, "--user") == SANDBOX_USER
    assert "--add-host" in run_command
    assert "host.docker.internal:172.17.0.1" in run_command
    assert "MACO_GATEWAY_URL=http://host.docker.internal:9/" in run_command
    assert "MACO_GATEWAY_TOKEN" in run_command
    assert "MACO_GATEWAY_TOKEN=secret-token" not in run_command
    assert not any(part == "-v" or str(context.workspace) in part for part in run_command)
    assert run_kwargs["env"]["MACO_GATEWAY_TOKEN"] == "secret-token"

    bootstrap_command = calls[1][0]
    assert bootstrap_command == [
        "docker-test",
        "exec",
        "--user",
        SANDBOX_USER,
        "container-123",
        "maco",
        "sandbox-bootstrap",
        "--workspace",
        "/workspace/macosdk",
    ]

    exec_command, exec_kwargs = calls[2]
    assert exec_command == [
        "docker-test",
        "exec",
        "--user",
        SANDBOX_USER,
        "-w",
        "/workspace",
        "container-123",
        "sh",
        "-lc",
        "echo hi",
    ]
    assert exec_kwargs["timeout"] == 7

    provider.stop()
    assert calls[-1][0] == ["docker-test", "rm", "-f", "container-123"]


def test_docker_provider_without_gateway_ip_preserves_desktop_host_alias(tmp_path, monkeypatch):
    context = _context(tmp_path)
    provider = DockerSandboxProvider(
        context,
        image="maco-test:latest",
        docker_binary="docker-test",
        gateway_host="host.docker.internal",
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[:3] == ["docker-test", "run", "-d"]:
            return subprocess.CompletedProcess(command, 0, stdout="container-123\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(docker_provider.subprocess, "run", fake_run)

    provider.start()

    run_command = calls[0][0]
    assert "--add-host" not in run_command
    assert "MACO_GATEWAY_URL=http://host.docker.internal:9/" in run_command

    provider.stop()
    assert calls[-1][0] == ["docker-test", "rm", "-f", "container-123"]


def test_docker_provider_stop_uses_container_name_before_id_is_known(tmp_path, monkeypatch):
    context = _context(tmp_path)
    provider = DockerSandboxProvider(context, image="maco-test:latest", docker_binary="docker-test")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[:3] == ["docker-test", "run", "-d"]:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_provider.subprocess, "run", fake_run)

    with pytest.raises(KeyboardInterrupt):
        provider.start()
    provider.stop()

    assert calls[-1] == (
        ["docker-test", "rm", "-f", provider.container_name],
        {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": 10,
            "check": False,
        },
    )


def test_docker_provider_write_file_writes_inside_container(tmp_path, monkeypatch):
    context = _context(tmp_path)
    provider = DockerSandboxProvider(context, image="maco-test:latest", docker_binary="docker-test")
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[:3] == ["docker-test", "run", "-d"]:
            return subprocess.CompletedProcess(command, 0, stdout="container-123\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(docker_provider.subprocess, "run", fake_run)

    guest_path = provider.write_file("nested/task.py", "print('ok')\n")

    assert guest_path == "/workspace/nested/task.py"
    write_call = calls[-1]
    assert write_call[0][:7] == ["docker-test", "exec", "-i", "--user", SANDBOX_USER, "container-123", "sh"]
    assert write_call[1]["input"] == "print('ok')\n"


def test_provider_factory_uses_default_sandbox_image(tmp_path):
    from maco.sandbox import provider_from_name

    context = _context(tmp_path)

    docker = provider_from_name("docker", context)
    matchlock = provider_from_name("matchlock", context)

    assert isinstance(docker, DockerSandboxProvider)
    assert isinstance(matchlock, MatchlockSandboxProvider)
    assert docker.image == DEFAULT_SANDBOX_IMAGE
    assert matchlock.image == DEFAULT_SANDBOX_IMAGE


def test_sandbox_image_version_uses_package_version(monkeypatch):
    monkeypatch.setattr(maco, "__version__", "4.5.6")

    assert sandbox_core._maco_version() == "4.5.6"


def test_sandbox_image_version_never_falls_back_to_zero(monkeypatch):
    monkeypatch.setattr(maco, "__version__", "")

    with pytest.raises(SandboxError, match="cannot determine the default sandbox image"):
        sandbox_core._maco_version()


def test_provider_factory_allows_explicit_image_when_default_version_is_unknown(monkeypatch, tmp_path):
    def fail_default_sandbox_image() -> str:
        raise SandboxError("missing version")

    monkeypatch.setattr(sandbox_core, "default_sandbox_image", fail_default_sandbox_image)
    context = _context(tmp_path)

    docker = sandbox_core.provider_from_name("docker", context, image="custom:latest")
    assert isinstance(docker, DockerSandboxProvider)
    assert docker.image == "custom:latest"

    with pytest.raises(SandboxError, match="missing version"):
        sandbox_core.provider_from_name("docker", context)


def test_matchlock_provider_explains_optional_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, "matchlock", None)

    with pytest.raises(SandboxError, match=r"mcp-as-code\[matchlock\]"):
        matchlock_provider._load_matchlock_sdk()


def test_matchlock_provider_uses_sdk_builder_without_leaking_token(tmp_path, monkeypatch, capsys):
    context = _context(tmp_path, debug=True)
    provider = MatchlockSandboxProvider(
        context,
        image="maco-test:latest",
        matchlock_binary="matchlock-test",
        gateway_host="maco-gateway.internal",
        extra_allow_hosts=["api.example.com"],
    )
    captured: dict[str, Any] = {}

    class FakeSandbox:
        def __init__(self, image: str) -> None:
            captured["image"] = image
            self.env: dict[str, str] = {}
            self.allowed: list[str] = []
            self.added_hosts: list[tuple[str, str]] = []
            self.secrets: list[tuple[str, str, str, tuple[str, ...]]] = []
            self.mounts: list[tuple[str, str, str, bool]] = []
            self.memory_mounts: list[str] = []

        def with_workspace(self, path: str) -> FakeSandbox:
            captured["workspace"] = path
            return self

        def with_user(self, user: str) -> FakeSandbox:
            captured["user"] = user
            return self

        def with_env_map(self, env: dict[str, str]) -> FakeSandbox:
            self.env.update(env)
            return self

        def with_env(self, name: str, value: str) -> FakeSandbox:
            self.env[name] = value
            return self

        def allow_host(self, host: str) -> FakeSandbox:
            self.allowed.append(host)
            return self

        def add_host(self, host: str, ip: str) -> FakeSandbox:
            self.added_hosts.append((host, ip))
            return self

        def add_secret_with_placeholder(
            self, name: str, value: str, placeholder: str, *hosts: str
        ) -> FakeSandbox:
            self.secrets.append((name, value, placeholder, hosts))
            return self

        def mount_host_dir_readonly(self, guest_path: str, host_path: str) -> FakeSandbox:
            self.mounts.append((guest_path, host_path, "host_fs", True))
            return self

        def mount_host_dir(self, guest_path: str, host_path: str) -> FakeSandbox:
            self.mounts.append((guest_path, host_path, "host_fs", False))
            return self

        def mount_memory(self, guest_path: str) -> FakeSandbox:
            self.memory_mounts.append(guest_path)
            return self

    class FakeConfig:
        def __init__(self, binary_path: str) -> None:
            captured["binary_path"] = binary_path

    class FakeClient:
        def __init__(self, config: FakeConfig) -> None:
            captured["config"] = config

        def start(self) -> None:
            captured["started"] = True

        def close(self) -> None:
            captured["closed"] = True

        def launch(self, spec: FakeSandbox) -> str:
            captured["spec"] = spec
            return "vm-test"

        def exec(self, command: str, *, working_dir: str, timeout: int) -> SandboxRunResult:
            captured.setdefault("execs", []).append((command, working_dir, timeout))
            return SandboxRunResult(0, "ok", "")

        def remove(self) -> None:
            captured["removed"] = True

    monkeypatch.setattr(matchlock_provider, "_load_matchlock_sdk", lambda: (FakeClient, FakeConfig, FakeSandbox))

    result = provider.run(SandboxExec(command="python task.py", timeout=11))

    assert result.ok
    spec = captured["spec"]
    assert captured["image"] == "maco-test:latest"
    assert captured["binary_path"] == "matchlock-test"
    assert captured["workspace"] == "/workspace"
    assert captured["user"] == SANDBOX_USER
    assert spec.allowed == ["api.example.com", "maco-gateway.internal"]
    assert spec.added_hosts == []
    assert spec.env["MACO_GATEWAY_URL"] == "http://maco-gateway.internal:9/"
    assert spec.env["MACO_GATEWAY_TOKEN"] == "MACO_GATEWAY_TOKEN_PLACEHOLDER"
    assert spec.secrets == [
        (
            "MACO_GATEWAY_TOKEN",
            "secret-token",
            "MACO_GATEWAY_TOKEN_PLACEHOLDER",
            ("maco-gateway.internal",),
        )
    ]
    assert spec.mounts == []
    assert spec.memory_mounts == ["/workspace"]
    assert captured["execs"] == [
        ("maco sandbox-bootstrap --workspace /workspace/macosdk", "/workspace", context.timeout),
        ("python task.py", "/workspace", 11),
    ]
    provider.stop()
    assert captured["removed"] is True
    err = capsys.readouterr().err
    assert "maco matchlock command:" in err
    assert "secret-token" not in err


def test_matchlock_provider_uses_explicit_gateway_ip_mapping(tmp_path, monkeypatch, capsys):
    context = _context(tmp_path, debug=True)
    provider = MatchlockSandboxProvider(
        context,
        image="maco-test:latest",
        gateway_ip="192.0.2.10",
    )
    captured: dict[str, Any] = {}

    class FakeSandbox:
        def __init__(self, _image: str) -> None:
            self.added_hosts: list[tuple[str, str]] = []
            self.allowed: list[str] = []
            self.secrets: list[tuple[str, ...]] = []

        def with_workspace(self, _path: str) -> FakeSandbox:
            return self

        def with_user(self, _user: str) -> FakeSandbox:
            return self

        def with_env_map(self, _env: dict[str, str]) -> FakeSandbox:
            return self

        def with_env(self, _name: str, _value: str) -> FakeSandbox:
            return self

        def allow_host(self, host: str) -> FakeSandbox:
            self.allowed.append(host)
            return self

        def add_host(self, host: str, ip: str) -> FakeSandbox:
            self.added_hosts.append((host, ip))
            return self

        def add_secret_with_placeholder(self, *args: str) -> FakeSandbox:
            self.secrets.append(args)
            return self

        def mount_host_dir_readonly(self, _guest_path: str, _host_path: str) -> FakeSandbox:
            return self

        def mount_host_dir(self, _guest_path: str, _host_path: str) -> FakeSandbox:
            return self

        def mount_memory(self, _guest_path: str) -> FakeSandbox:
            return self

    class FakeConfig:
        def __init__(self, binary_path: str) -> None:
            captured["binary_path"] = binary_path

    class FakeClient:
        def __init__(self, _config: FakeConfig) -> None:
            pass

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def launch(self, spec: FakeSandbox) -> str:
            captured["spec"] = spec
            return "vm-test"

        def exec(self, command: str, *, working_dir: str, timeout: int) -> SandboxRunResult:
            captured.setdefault("execs", []).append((command, working_dir, timeout))
            return SandboxRunResult(0, "ok", "")

        def remove(self) -> None:
            pass

    monkeypatch.setattr(matchlock_provider, "_load_matchlock_sdk", lambda: (FakeClient, FakeConfig, FakeSandbox))
    provider.run(SandboxExec(command="true"))

    assert captured["spec"].added_hosts == [("maco-gateway.internal", "192.0.2.10")]
    assert captured["spec"].allowed == []
    assert captured["spec"].secrets == []
    assert captured["execs"] == [
        ("maco sandbox-bootstrap --workspace /workspace/macosdk", "/workspace", context.timeout),
        ("true", "/workspace", context.timeout),
    ]
    assert "hosts=192.0.2.10:maco-gateway.internal" in capsys.readouterr().err


def test_matchlock_provider_cleans_up_interrupted_launch(tmp_path, monkeypatch):
    context = _context(tmp_path)
    provider = MatchlockSandboxProvider(context, image="maco-test:latest", matchlock_binary="matchlock-test")
    captured: dict[str, Any] = {}

    class FakeSandbox:
        def __init__(self, _image: str) -> None:
            pass

        def with_workspace(self, _path: str) -> FakeSandbox:
            return self

        def with_user(self, _user: str) -> FakeSandbox:
            return self

        def with_env_map(self, _env: dict[str, str]) -> FakeSandbox:
            return self

        def with_env(self, _name: str, _value: str) -> FakeSandbox:
            return self

        def allow_host(self, _host: str) -> FakeSandbox:
            return self

        def add_host(self, _host: str, _ip: str) -> FakeSandbox:
            return self

        def add_secret_with_placeholder(self, *_args: str) -> FakeSandbox:
            return self

        def mount_memory(self, _guest_path: str) -> FakeSandbox:
            return self

    class FakeConfig:
        def __init__(self, binary_path: str) -> None:
            captured["binary_path"] = binary_path

    class FakeClient:
        def __init__(self, _config: FakeConfig) -> None:
            pass

        def start(self) -> None:
            captured["started"] = True

        def launch(self, _spec: FakeSandbox) -> str:
            raise KeyboardInterrupt

        def close(self) -> None:
            captured["closed"] = True

        def remove(self) -> None:
            captured["removed"] = True

    monkeypatch.setattr(matchlock_provider, "_load_matchlock_sdk", lambda: (FakeClient, FakeConfig, FakeSandbox))

    with pytest.raises(KeyboardInterrupt):
        provider.start()

    assert captured == {
        "binary_path": "matchlock-test",
        "started": True,
        "closed": True,
        "removed": True,
    }
    assert provider.client is None


def test_write_code_file_and_guest_path_are_constrained(tmp_path):
    scratch = tmp_path / "scratch"
    path = write_code_file(scratch, "nested/task.py", "print('ok')\n")

    assert path.read_text(encoding="utf-8") == "print('ok')\n"
    assert guest_path_for(path, scratch, "/workspace") == "/workspace/nested/task.py"
    with pytest.raises(SandboxError, match="relative path"):
        write_code_file(scratch, "../escape.py", "")


def _context(tmp_path: Path, *, debug: bool = False) -> SandboxContext:
    workspace = tmp_path / ".maco"
    (workspace / "maco_generated").mkdir(parents=True)
    (workspace / "maco_generated" / "client.py").write_text("", encoding="utf-8")
    (workspace / "gateway.json").write_text(
        json.dumps({"url": "http://127.0.0.1:9/", "token": "secret-token"}),
        encoding="utf-8",
    )
    return SandboxContext(
        workspace=workspace.resolve(),
        scratch=(tmp_path / "scratch").resolve(),
        gateway=GatewayInfo.from_file(workspace / "gateway.json"),
        debug=debug,
    )


def _flag_value(command: list[str], flag: str) -> str:
    index = command.index(flag)
    return command[index + 1]
