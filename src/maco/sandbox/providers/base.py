"""Shared helpers for concrete sandbox providers."""

from __future__ import annotations

from pathlib import Path
import shlex
from typing import Mapping

from ..core import SANDBOX_SDK_ROOT, SandboxContext, SandboxError, SandboxExec, normalize_context, posix_join


class BaseSandboxProvider:
    guest_workspace: str
    guest_scratch: str
    default_python_command = "python"

    def __init__(self, context: SandboxContext) -> None:
        self.context = normalize_context(context)

    def start(self) -> None:
        """Start provider resources. Local/one-shot providers can no-op."""

    def stop(self) -> None:
        """Release provider resources. Local/one-shot providers can no-op."""

    def write_file(self, relative_path: str, content: str) -> str:
        path = self._local_scratch_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return self._guest_scratch_path(relative_path)

    def python_script_command(self, guest_script_path: str, args: list[str]) -> str:
        command = self.context.python_command or self.default_python_command
        return " ".join([command, shlex.quote(guest_script_path), *[shlex.quote(arg) for arg in args]])

    def _timeout(self, request: SandboxExec) -> int:
        return request.timeout or self.context.timeout

    def _guest_env(self, request_env: Mapping[str, str], *, gateway_url: str) -> dict[str, str]:
        env = {
            "MACO_WORKSPACE": self.guest_workspace,
            "MACO_GATEWAY_FILE": posix_join(self.guest_workspace, "gateway.json"),
            "MACO_GATEWAY_URL": gateway_url,
            "PYTHONPATH": self.guest_workspace,
        }
        if self.context.gateway.token:
            env["MACO_GATEWAY_TOKEN"] = self.context.gateway.token
        env.update(request_env)
        return env

    def _local_scratch_path(self, relative_path: str) -> Path:
        relative = self._relative_scratch_path(relative_path)
        return self.context.scratch / relative

    def _guest_scratch_path(self, relative_path: str) -> str:
        return posix_join(self.guest_scratch, self._relative_scratch_path(relative_path).as_posix())

    def _relative_scratch_path(self, relative_path: str) -> Path:
        if not relative_path.strip():
            raise SandboxError("path must be non-empty")
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise SandboxError("path must be relative and inside the sandbox scratch directory")
        return relative

class RemoteSandboxProvider(BaseSandboxProvider):
    guest_workspace = SANDBOX_SDK_ROOT
    guest_scratch = "/workspace"
