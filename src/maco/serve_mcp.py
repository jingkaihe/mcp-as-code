"""HTTP MCP server for running maco code inside a sandbox provider."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading
from types import FrameType
from typing import Annotated, Any

from jinja2 import Environment, PackageLoader, StrictUndefined
from mcp.server.fastmcp import FastMCP
from pydantic import Field
from starlette.responses import JSONResponse

from .codegen import fetch_gateway_tools, generate_sandbox_sdk, server_module_names
from .config import load_config
from .gateway import GatewayServer, ServeOptions
from .sandbox import (
    GatewayInfo,
    SandboxContext,
    SandboxExec,
    SandboxProvider,
    SandboxRunResult,
    default_matchlock_gateway_ip,
    provider_from_name,
)
from .service import SERVICE_IDENTITY_PATH


_TEMPLATES = Environment(
    loader=PackageLoader("maco", "templates"),
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=StrictUndefined,
)
_MANAGED_GATEWAY_STARTED_TEMPLATE = _TEMPLATES.from_string(
    """\
maco gateway started
  URL: {{ gateway.url }}
{% for url in gateway.extra_urls %}
  extra URL: {{ url }}
{% endfor %}
  gateway file: {{ gateway.gateway_file }}
"""
)
_LOCAL_SDK_GENERATED_TEMPLATE = _TEMPLATES.from_string(
    """\
Generated local sandbox SDK with {{ stats.tool_count }} tools from {{ stats.server_count }} servers
SDK workspace: {{ stats.workspace }}
"""
)
_MCP_SERVER_STARTED_TEMPLATE = _TEMPLATES.from_string(
    """\
maco MCP server started
  URL: http://{{ options.host }}:{{ options.port }}/mcp
  provider: {{ options.provider }}
  SDK: {{ provider.guest_workspace }}
  scratch: {{ scratch }}
"""
)


@dataclass(frozen=True)
class ServeMcpOptions:
    """Configuration for the sandbox-backed MCP server."""

    config: str | Path = "mcp.json"
    provider: str = "local"
    workspace: str | Path = ".maco"
    clean: bool = False
    scratch: str | Path | None = None
    gateway_host: str | None = None
    gateway_port: int = 0
    gateway_token: str | None = None
    gateway_use_token: bool = True
    host: str = "127.0.0.1"
    port: int = 8789
    timeout: int = 60
    debug: bool = False
    image: str | None = None
    python_command: str | None = None
    docker_binary: str = "docker"
    docker_network: str | None = None
    docker_gateway_host: str = "host.docker.internal"
    docker_gateway_ip: str | None = None
    matchlock_binary: str = "matchlock"
    matchlock_gateway_host: str = "maco-gateway.internal"
    matchlock_gateway_ip: str | None = None
    matchlock_allow_host: tuple[str, ...] = ()
    detached_service_id: str | None = None
    detached_service_token: str | None = None


class _ServeMcpShutdown(KeyboardInterrupt):
    """Raised from signal handlers so provider cleanup can run."""


def _install_shutdown_signal_handlers() -> tuple[tuple[int, Any], ...]:
    if threading.current_thread() is not threading.main_thread():
        return ()

    def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
        print(f"\nreceived signal {signum}; stopping maco MCP server", file=sys.stderr)
        raise _ServeMcpShutdown

    return (
        (signal.SIGINT, signal.signal(signal.SIGINT, _request_shutdown)),
        (signal.SIGTERM, signal.signal(signal.SIGTERM, _request_shutdown)),
    )


def _restore_signal_handlers(handlers: tuple[tuple[int, Any], ...]) -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    for signum, handler in handlers:
        signal.signal(signum, handler)


def serve_mcp(options: ServeMcpOptions) -> None:
    """Run a streamable HTTP MCP server exposing sandboxed bash/code tools."""

    gateway_server: GatewayServer | None = None
    provider: SandboxProvider | None = None
    old_signal_handlers = _install_shutdown_signal_handlers()
    try:
        workspace = Path(options.workspace).expanduser().resolve()
        scratch = (
            Path(options.scratch).expanduser().resolve()
            if options.scratch is not None
            else default_scratch_path(workspace)
        )
        normalized_provider = _normalize_provider(options.provider)
        docker_gateway_ip = (
            _docker_gateway_ip(
                options.docker_gateway_ip,
                docker_binary=options.docker_binary,
                docker_network=options.docker_network,
            )
            if normalized_provider == "docker"
            else None
        )
        matchlock_gateway_ip = (
            options.matchlock_gateway_ip or default_matchlock_gateway_ip()
            if normalized_provider == "matchlock"
            else None
        )
        config = load_config(options.config)
        gateway_host = options.gateway_host or _default_gateway_host()
        _validate_managed_gateway_bind(
            normalized_provider,
            explicit_gateway_host=options.gateway_host,
            matchlock_gateway_ip=matchlock_gateway_ip,
        )
        extra_hosts = _gateway_extra_hosts(
            normalized_provider,
            docker_gateway_ip=docker_gateway_ip,
            matchlock_gateway_ip=matchlock_gateway_ip,
            explicit_gateway_host=options.gateway_host,
        )
        gateway_server = GatewayServer(
            config,
            ServeOptions(
                host=gateway_host,
                port=options.gateway_port,
                workspace=workspace,
                token=options.gateway_token,
                use_token=options.gateway_use_token,
                extra_hosts=extra_hosts,
                freebind_hosts=_gateway_freebind_hosts(
                    gateway_host,
                    extra_hosts=extra_hosts,
                    docker_gateway_ip=docker_gateway_ip,
                    matchlock_gateway_ip=matchlock_gateway_ip,
                ),
            ),
        ).start()
        print(_MANAGED_GATEWAY_STARTED_TEMPLATE.render(gateway=gateway_server).strip())
        gateway = GatewayInfo(url=gateway_server.url, token=gateway_server.token)
        tools_by_server = fetch_gateway_tools(gateway.url, token=gateway.token)
        modules = sorted(server_module_names(tools_by_server.keys()).values())
        if normalized_provider == "local":
            _clean_local_sdk(workspace)
            stats = generate_sandbox_sdk(tools_by_server, workspace=workspace, clean=False)
            print(_LOCAL_SDK_GENERATED_TEMPLATE.render(stats=stats).strip())
        context = SandboxContext(
            workspace=workspace,
            scratch=scratch,
            gateway=gateway,
            timeout=options.timeout,
            python_command=options.python_command,
            debug=options.debug,
        )
        provider = provider_from_name(
            options.provider,
            context,
            image=options.image,
            docker_binary=options.docker_binary,
            docker_network=options.docker_network,
            docker_gateway_host=options.docker_gateway_host,
            docker_gateway_ip=docker_gateway_ip,
            matchlock_binary=options.matchlock_binary,
            matchlock_gateway_host=options.matchlock_gateway_host,
            matchlock_gateway_ip=matchlock_gateway_ip,
            matchlock_extra_allow_hosts=list(options.matchlock_allow_host),
        )
        provider.start()
        app = create_serve_mcp_app(
            provider,
            context,
            server_modules=modules,
            host=options.host,
            port=options.port,
            detached_service_id=options.detached_service_id,
            detached_service_token=options.detached_service_token,
        )
        print(_MCP_SERVER_STARTED_TEMPLATE.render(options=options, provider=provider, scratch=scratch).strip())
        app.run("streamable-http")
    except _ServeMcpShutdown:
        pass
    finally:
        _restore_signal_handlers(old_signal_handlers)
        try:
            if provider is not None:
                provider.stop()
        finally:
            if gateway_server is not None:
                gateway_server.stop()


def create_serve_mcp_app(
    provider: SandboxProvider,
    context: SandboxContext,
    *,
    server_modules: list[str] | None = None,
    host: str = "127.0.0.1",
    port: int = 8789,
    detached_service_id: str | None = None,
    detached_service_token: str | None = None,
) -> FastMCP:
    """Create the MCP app used by ``serve_mcp``.

    Separated for tests and future embedding. Tools intentionally return plain
    JSON-serializable dictionaries so MCP clients can inspect exit status,
    stdout, and stderr.
    """

    app = FastMCP(
        "maco",
        instructions=_mcp_instructions(provider, context, server_modules=server_modules),
        host=host,
        port=port,
        json_response=True,
        stateless_http=True,
    )

    if detached_service_id and detached_service_token:

        @app.custom_route(SERVICE_IDENTITY_PATH, methods=["GET"], include_in_schema=False)
        async def identity(_request: Any) -> JSONResponse:
            return JSONResponse(
                {
                    "id": detached_service_id,
                    "identity_token": detached_service_token,
                }
            )

    @app.tool(description=_bash_description(provider, context, server_modules=server_modules))
    def bash(
        command: Annotated[
            str,
            Field(description="Non-interactive shell command to run in the sandbox scratch directory."),
        ],
        timeout: Annotated[
            int | None,
            Field(description="Optional command timeout in seconds. Omit to use the server default."),
        ] = None,
    ) -> dict[str, Any]:
        result = provider.run(SandboxExec(command=command, timeout=timeout))
        return _result_payload(result)

    @app.tool(description=_code_execute_description(provider, context, server_modules=server_modules))
    def code_execute(
        code: Annotated[
            str,
            Field(
                description=(
                    "Python source code to write into the sandbox scratch directory and execute. "
                    "Import generated tools from tools.<server>."
                )
            ),
        ],
        filename: Annotated[
            str | None,
            Field(
                description=(
                    "Optional relative .py path in scratch. Leave null to use <hash>.py."
                )
            ),
        ] = None,
        args: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Optional command-line arguments passed as sys.argv[1:]. "
                    "Leave null for no arguments."
                )
            ),
        ] = None,
        timeout: Annotated[
            int | None,
            Field(description="Optional script timeout in seconds. Omit to use the server default."),
        ] = None,
    ) -> dict[str, Any]:
        filename = filename if filename is not None else _content_addressed_script_filename(code)
        guest_script = provider.write_file(filename, code)
        command = provider.python_script_command(guest_script, args or [])
        result = provider.run(SandboxExec(command=command, timeout=timeout))
        payload = _result_payload(result)
        payload["script"] = guest_script
        payload["guest_script"] = guest_script
        return payload

    return app


def default_scratch_path(workspace: Path) -> Path:
    """Return the default writable scratch directory for a maco workspace."""

    return workspace / "scratch"


def _mcp_instructions(
    provider: SandboxProvider,
    context: SandboxContext,
    *,
    server_modules: list[str] | None = None,
) -> str:
    return _render_model_text("serve_mcp_instructions.j2", provider, context, server_modules=server_modules)


def _bash_description(
    provider: SandboxProvider,
    context: SandboxContext,
    *,
    server_modules: list[str] | None = None,
) -> str:
    return _render_model_text("bash_description.j2", provider, context, server_modules=server_modules)


def _code_execute_description(
    provider: SandboxProvider,
    context: SandboxContext,
    *,
    server_modules: list[str] | None = None,
) -> str:
    return _render_model_text("code_execute_description.j2", provider, context, server_modules=server_modules)


def _render_model_text(
    template_name: str,
    provider: SandboxProvider,
    context: SandboxContext,
    *,
    server_modules: list[str] | None = None,
) -> str:
    wrapper_root = _guest_tools_root(provider)
    return _TEMPLATES.get_template(template_name).render(
        server_modules=server_modules if server_modules is not None else _server_modules(context.workspace),
        wrapper_root=wrapper_root,
    ).strip()


def _server_modules(workspace: Path) -> list[str]:
    server_root = workspace / "tools"
    return sorted(
        path.name
        for path in server_root.iterdir()
        if path.is_dir() and (path / "__init__.py").exists()
    ) if server_root.exists() else []


def _guest_tools_root(provider: SandboxProvider) -> str:
    return f"{provider.guest_workspace.rstrip('/')}/tools"


def _clean_local_sdk(workspace: Path) -> None:
    for path in [workspace / "tools", workspace / "manifest.json", workspace / "pyproject.toml"]:
        if path.is_dir():
            import shutil

            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def _content_addressed_script_filename(code: str) -> str:
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
    return f"{digest}.py"


def _default_gateway_host() -> str:
    return "127.0.0.1"


def _supports_freebind() -> bool:
    return sys.platform.startswith("linux")


def _validate_managed_gateway_bind(
    provider: str,
    *,
    explicit_gateway_host: str | None,
    matchlock_gateway_ip: str | None,
) -> None:
    if provider != "matchlock" or explicit_gateway_host or not matchlock_gateway_ip or _supports_freebind():
        return
    raise ValueError(
        "matchlock managed gateway requires an explicit --gateway-host on this platform; "
        "use --gateway-host 0.0.0.0 to expose the gateway to the sandbox"
    )


def _normalize_provider(provider: str) -> str:
    return provider.replace("_", "-").lower()


def _docker_gateway_ip(
    configured_ip: str | None,
    *,
    docker_binary: str,
    docker_network: str | None,
) -> str | None:
    if configured_ip:
        return configured_ip
    if _is_docker_desktop(docker_binary):
        return None
    detected_ip = _detect_docker_gateway_ip(docker_binary, docker_network)
    if detected_ip:
        return detected_ip
    network = docker_network or "bridge"
    raise ValueError(
        f"could not detect Docker gateway IP for network {network!r}; "
        "pass --docker-gateway-ip explicitly"
    )


def _is_docker_desktop(docker_binary: str) -> bool:
    if not sys.platform.startswith("linux"):
        return True
    try:
        completed = subprocess.run(
            [docker_binary, "info", "--format", "{{.OperatingSystem}}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    if completed.returncode != 0:
        return False
    return "docker desktop" in completed.stdout.strip().lower()


def _detect_docker_gateway_ip(docker_binary: str, docker_network: str | None) -> str | None:
    network = docker_network or "bridge"
    try:
        completed = subprocess.run(
            [docker_binary, "network", "inspect", network],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    try:
        networks = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(networks, list) or not networks or not isinstance(networks[0], dict):
        return None
    ipam = networks[0].get("IPAM")
    configs = ipam.get("Config") if isinstance(ipam, dict) else None
    if not isinstance(configs, list):
        return None
    for config in configs:
        if not isinstance(config, dict):
            continue
        gateway = config.get("Gateway")
        if isinstance(gateway, str) and gateway and "." in gateway:
            return gateway
    return None


def _gateway_extra_hosts(
    provider: str,
    *,
    docker_gateway_ip: str | None,
    matchlock_gateway_ip: str | None,
    explicit_gateway_host: str | None,
) -> tuple[str, ...]:
    gateway_ip = _provider_gateway_ip(provider, docker_gateway_ip, matchlock_gateway_ip)
    if not gateway_ip:
        return ()
    if explicit_gateway_host and explicit_gateway_host not in {"127.0.0.1", "localhost", "::1"}:
        return ()
    return (gateway_ip,)


def _gateway_freebind_hosts(
    gateway_host: str,
    *,
    extra_hosts: tuple[str, ...],
    docker_gateway_ip: str | None,
    matchlock_gateway_ip: str | None,
) -> tuple[str, ...]:
    hosts = list(extra_hosts)
    if gateway_host in {docker_gateway_ip, matchlock_gateway_ip}:
        hosts.append(gateway_host)
    return tuple(dict.fromkeys(hosts))


def _provider_gateway_ip(
    provider: str,
    docker_gateway_ip: str | None,
    matchlock_gateway_ip: str | None,
) -> str | None:
    if provider == "docker":
        return docker_gateway_ip
    if provider == "matchlock":
        return matchlock_gateway_ip
    return None


def _result_payload(result: SandboxRunResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
