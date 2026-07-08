from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen


def test_serve_generates_wrappers_and_run_end_to_end(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    config_path = tmp_path / "mcp.json"
    workspace = tmp_path / ".maco"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "echo-server": {
                        "command": "uv",
                        "args": [
                            "run",
                            "--project",
                            str(repo),
                            "python",
                            str(repo / "tests" / "fixtures" / "echo_mcp_server.py"),
                        ],
                    }
                }
            }
        )
    )

    script = tmp_path / "use_tools.py"
    script.write_text(
        """
import asyncio
from maco_generated.servers.echo_server import AddInput, EchoInput, add, echo

async def main():
    message, sum_result = await asyncio.gather(
        echo(EchoInput(message="hello from code")),
        add(AddInput(a=2, b=5)),
    )
    print(message.result)
    print(sum_result.result)

asyncio.run(main())
""".strip()
        + "\n"
    )

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    server = subprocess.Popen(
        [
            "uv",
            "run",
            "--project",
            str(repo),
            "maco",
            "_gateway",
            "--config",
            str(config_path),
            "--workspace",
            str(workspace),
            "--clean",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        gateway_file = workspace / "gateway.json"
        deadline = time.time() + 30
        last_output = ""
        gateway = None
        while time.time() < deadline:
            if server.poll() is not None:
                last_output += server.stdout.read() if server.stdout else ""
                raise AssertionError(f"maco _gateway exited early:\n{last_output}")
            if server.stdout:
                # Drain a little without blocking only after file exists; output is useful on failure.
                pass
            if gateway_file.exists():
                gateway = json.loads(gateway_file.read_text())
                try:
                    with urlopen(gateway["url"] + "health", timeout=1) as response:
                        if response.status == 200:
                            break
                except Exception:
                    pass
            time.sleep(0.1)
        else:
            if server.stdout:
                last_output += server.stdout.read()
            raise AssertionError(f"maco _gateway did not become healthy:\n{last_output}")

        completed = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(repo),
                "maco",
                "run",
                "--workspace",
                str(workspace),
                str(script),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip().splitlines() == ["hello from code", "7"]
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        if server.stdout:
            server.stdout.close()
