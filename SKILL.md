---
name: maco
description: Use maco to access many MCP tools through a compact code-execution interface. Trigger when users want to run or configure maco up, use MCP tools through bash/code_execute, run multi-step MCP workflows in Python, filter/join/page large MCP responses locally, or reduce MCP tool-schema context.
---

# maco

`maco` lets agents use many upstream MCP tools through a small code-execution surface. It follows the code-execution-with-MCP pattern: keep live MCP sessions and large schemas behind a gateway, then use Python for multi-step work without loading every tool definition into context.

## Primary interface: `maco up`

Prefer MCP mode whenever possible. `maco up` starts a managed gateway for the upstream MCP servers and exposes one Streamable HTTP MCP endpoint with two tools:

- `bash(command, timeout?)` — inspect generated wrappers or run small non-interactive probes in the sandbox.
- `code_execute(code, filename?, args?, timeout?)` — run Python that imports generated MCP tools.

Agents thrive on discovery with `rg` and `fd`, so maco gives them `bash` access to navigate the tool interface as a real filesystem before writing code.

Start it from this repository/skill directory or an installed package environment with:

```bash
uv run maco up --config mcp.json --provider local
```

Defaults:

- Config: `mcp.json`
- HTTP MCP endpoint: `http://127.0.0.1:8789/mcp`
- Generated host workspace: `.maco`
- Sandbox SDK: `/workspace/macosdk/tools`
- Sandbox providers: `local`, `docker`, `matchlock`

The `matchlock` provider requires the optional `mcp-as-code[matchlock]` extra and the Matchlock binary on `PATH`.

## Client/agent workflow

When connected to a `maco up` endpoint, do not try to enumerate every upstream MCP schema. Work progressively:

1. Use the MCP `bash` tool with `rg`/`fd` for code navigation inside the sandbox:

   ```bash
   rg --files /workspace/macosdk/tools
   sed -n '1,160p' /workspace/macosdk/tools/<server>/__init__.py
   sed -n '1,220p' /workspace/macosdk/tools/<server>/<tool>.py
   ```

   Use `rg --files ... | rg '<keyword>'` when you have a likely server or tool name.

2. Use `code_execute` to call tools in a context-efficient manner. Prefer loops, conditions, and local reduction over traditional linear tool-call chaining:

   ```python
   from tools.<server> import <list_tool>

   for query in ["open", "closed", "recent"]:
       result = <list_tool>(query=query)
       items = getattr(result, "items", result)

       if items:
           print(query, len(items))
       else:
           print(query, "no results")
   ```

3. Keep data reduction inside Python. Page, filter, join, deduplicate, and summarize locally before printing results back to the agent.

4. Prefer passing only the `code` argument to `code_execute`. Use `filename` only when a stable readable traceback path matters, and `args` only when the script explicitly reads command-line arguments.

## Config format

`maco` expects Claude-style JSON with a top-level `mcpServers` object:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    }
  }
}
```

Prefer listing required environment variables under `env`. `maco` expands `$VAR` and `${VAR}` from the environment of the `maco` process:

```json
{
  "mcpServers": {
    "github": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", "ghcr.io/github/github-mcp-server"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      }
    }
  }
}
```

HTTP and SSE servers can use Claude-style URL fields:

```json
{
  "mcpServers": {
    "remote": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {"Authorization": "Bearer ${TOKEN}"}
    }
  }
}
```

For remote HTTP/SSE MCP servers without static `Authorization` headers, OAuth is
auto-discovered from HTTP 401 Bearer challenges. Add an `oauth` object only for
providers that need pre-registered clients, scopes, or callback behavior:

```json
{
  "mcpServers": {
    "remote": {
      "type": "http",
      "url": "https://example.com/mcp",
      "oauth": {
        "client_id": "${MCP_CLIENT_ID}",
        "client_secret": "${MCP_CLIENT_SECRET}",
        "scopes": ["mcp.read"],
        "redirect_uri": "http://127.0.0.1:1456/mcp/oauth/callback",
        "interactive": "auto",
        "open_browser": true,
        "callback_timeout": "2m"
      }
    }
  }
}
```

Omit `oauth` entirely when dynamic client registration and standard discovery are
enough. Static `Authorization` headers take precedence and skip OAuth. Tokens are
cached under `~/.maco/mcp/oauth/`. For CI/headless runs set `oauth.interactive`
or `MACO_MCP_OAUTH_INTERACTIVE` to `never`; `MACO_MCP_OAUTH_OPEN_BROWSER` and
`MACO_MCP_OAUTH_CALLBACK_TIMEOUT` can override browser and timeout behavior.

Supported transports: `stdio`, `http`/`streamable_http`, and `sse`.

## When to use maco

Use maco when you need to:

- call several MCP tools in loops;
- page, filter, sort, or reduce large responses;
- join data across MCP servers;
- persist local intermediate files or helper code;
- avoid loading hundreds of direct MCP tool schemas into the agent context.

For one simple direct MCP call, direct tool use may still be faster.

## Safety notes

- `maco up` exposes shell/Python execution to connected MCP clients; bind it only where intended.
- The managed gateway uses a bearer token by default. Do not commit `.maco/gateway.json`.
- Sandbox providers change process/container/VM isolation, but upstream MCP servers still control their own permissions and side effects.
- Generated wrappers are code. Inspect unfamiliar wrappers before calling them.
