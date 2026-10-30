# Guacamole LLM Bridge

One Python package with two installed commands:

| Command | Machine | Purpose |
| --- | --- | --- |
| `guac-llm-client` | Windows, macOS, or Linux client PC | Log into Guacamole and expose an OpenAI-compatible HTTP endpoint on local loopback |
| `guac-llm-server` | Linux server running vLLM | Relay HTTP through the Guacamole SSH terminal using stdin/stdout |

Requires Python **3.11 or newer** and `uv`. The server helper uses only the Python
standard library; the shared package installs `aiohttp` for the client command.

This is an initial implementation. Automated integration tests use a simulated
Guacamole gateway and the actual helper in a Linux PTY. It has **not been tested
against a production Guacamole deployment**.

## Installation

Clone and install on both machines:

```shell
git clone https://github.com/himbeles/guac-llm-bridge.git
cd guac-llm-bridge
uv tool install .
uv tool update-shell
```

Alternatively, when using the downloadable archive, install its included universal wheel:

```shell
uv tool install ./dist/guac_llm_bridge-0.1.0-py3-none-any.whl
uv tool update-shell
```

Open a fresh terminal if required by `update-shell`. Both commands are installed
on both machines; use the appropriate one on each.

If necessary, select an existing suitable Python explicitly:

```shell
uv tool install --python /path/to/python3.11 ./dist/guac_llm_bridge-0.1.0-py3-none-any.whl
```

The package is supplied locally and has not been published to PyPI. Use the local
path, not `uv tool install guac-llm-bridge`.

## 1. Check the Linux server

Install the package through your existing Guacamole terminal. Then verify:

```shell
guac-llm-server --help
curl http://127.0.0.1:8000/v1/models
```

Keep your existing vLLM process running. **Do not start `guac-llm-server` manually
or as a background service.** The local client starts it inside its own Guacamole
terminal session and stops it on exit. The helper opens no network listener.

By default, the client starts `~/.local/bin/guac-llm-server`, uv's usual executable
location on Linux. If your installation uses a different location, obtain it with:

```shell
command -v guac-llm-server
```

Then pass that absolute path to the client with `--server-command`.

## 2. Start the client PC bridge

The following command works in PowerShell, Bash, and similar shells:

```shell
guac-llm-client --url "https://guacamole.example.com/guacamole/#/client/CLIENT_ID" --username YOUR_USERNAME
```

The password is requested without echo. The URL is a CLI argument and can be
changed freely. Replace the example URL with the full URL from your browser after
opening the desired Guacamole connection, and replace `YOUR_USERNAME` with your
Guacamole login. The fragment identifies the connection and its data source.
Root and subpath deployments such as `/guacamole/` are supported.

After `Remote helper ready` and `Local API: ...` appear, configure your LLM client:

| Setting | Value |
| --- | --- |
| OpenAI base URL | `http://127.0.0.1:8000/v1` |
| Chat completions URL | `http://127.0.0.1:8000/v1/chat/completions` |
| Model | Exact ID returned by `/v1/models` |
| API key | Your vLLM key, or any placeholder if vLLM does not require one |

For clients that ask for a full completions URL, use the second row, not just the
base URL. Stop the bridge with **Ctrl+C**. It only binds to `127.0.0.1`.

## 3. Test locally

```shell
curl http://127.0.0.1:8000/v1/models
```

On PowerShell, use `curl.exe` if `curl` resolves to a PowerShell alias.
Example streaming request in Python, using the OpenAI SDK in your own project:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="unused")
model = client.models.list().data[0].id
for chunk in client.chat.completions.create(
    model=model,
    messages=[{"role": "user", "content": "Hello."}],
    stream=True,
):
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="", flush=True)
```

`openai` is only required for this example; it is not a bridge dependency.

## CLI options

```shell
guac-llm-client --help
guac-llm-server --help
```

| Client option | Default | Meaning |
| --- | --- | --- |
| `--url` | Required | Guacamole URL with connection fragment |
| `--username` | Prompt | Guacamole username |
| `--port` | `8000` | Client PC loopback port |
| `--upstream` | `http://127.0.0.1:8000` | vLLM location as seen from the Linux server |
| `--server-command` | `~/.local/bin/guac-llm-server` | Installed Linux helper executable path |
| `--transport` | `auto` | Try WebSocket, then HTTP tunnel; force `websocket` or `http` if needed |
| `--startup-delay` | `2` | Seconds to allow shell startup before launching helper |
| `--connect-timeout` | `30` | Connection/startup/input acknowledgment timeout in seconds |
| `--request-timeout` | `1800` | Maximum complete HTTP request duration in seconds |
| `--max-requests` | `4` | Simultaneous requests, at most 16 |
| `--max-body-mib` | `16` | Maximum incoming HTTP body size, from 1 to 16 MiB |
| `--ca-file` | System trust | Corporate CA PEM bundle |
| `--insecure` | Disabled | Explicitly disable Guacamole TLS certificate verification |
| `--connection-id`, `--data-source` | From URL fragment | Select/override connection without a fragment |

`--upstream` is restricted to server loopback HTTP. An optional URL path is
prepended to the fixed API paths. For example, `http://127.0.0.1:8000/custom`
targets `/custom/v1/chat/completions`. Do not add `/v1` to the default upstream.

The server command accepts `--upstream`, `--request-timeout`, and `--max-requests`;
normally the client supplies all three automatically.

## Authentication and corporate networking

The standard Guacamole username/password API is supported. Browser-only SSO,
MFA challenges, and external login portals require additional integration.
Corporate HTTP proxy variables are honored on the client. TLS verification is
enabled by default. Example with a corporate CA:

```shell
guac-llm-client --url "https://your-guacamole/#/client/..." --ca-file company-ca.pem
```

Optional environment variables:

| Variable | Meaning |
| --- | --- |
| `GUAC_PASSWORD` | Supply Guacamole password instead of the interactive prompt |
| `VLLM_API_KEY` | Replace the forwarded Authorization header with this upstream vLLM key |
| `BRIDGE_API_KEY` | Require this Bearer key for the local API |

Without `VLLM_API_KEY`, the local request's Authorization header is forwarded to
vLLM unchanged. With `BRIDGE_API_KEY`, configure your local LLM application to
send that key; set `VLLM_API_KEY` separately if vLLM requires a different key.
Credentials, session tokens, and prompt bodies are not logged by the bridge.
Guacamole's own session recording, access logs, and administrator visibility still
apply. Base64 is framing, not encryption; use the HTTPS Guacamole URL.

## Transport behavior and limits

- Supports `GET /v1/models`, `POST /v1/chat/completions`, and
  `POST /v1/completions`, plus local `GET /health`. Other routes return 404.
- JSON responses and SSE response bytes are relayed without interpreting model
  output, tool calls, or reasoning fields. Query parameters are currently rejected.
- STDIN uses Guacamole's documented pipe interface. Server output uses OSC
  `482202;2;LLM`, with automatic pipe flushing enabled for low latency. Payloads
  are Base64-framed so terminal escape processing cannot corrupt model data.
- The helper switches its PTY to raw mode and restores settings on orderly exit.
- Per-request response credits bound buffering for slow local clients. A slow
  request does not block all other response streams.
- Cancellation closes the corresponding upstream connection. Whether GPU work is
  immediately released depends on vLLM's disconnect handling.
- Tunnel loss aborts active responses. Requests are never silently retried. Restart
  the client to establish a fresh session; automatic reconnection is not implemented.
- The same terminal must be available as a Bash shell after login. A menu, another
  SSH login prompt, or an attached foreground program requires adaptation.
- The Guacamole connection must allow terminal input/output pipes. Clipboard or
  connection policies and older/custom versions may affect compatibility.
- Throughput is lower than a direct HTTP connection. Large contexts incur Base64,
  PTY, Guacamole protocol, and acknowledgment overhead.

If the helper does not become ready, first check its installed path, then try
`--startup-delay 5`. If WebSocket fails through a proxy, try `--transport http`.
If the local port is occupied, use `--port 18000` and update the local base URL.

## Development and validation

```shell
uv sync
uv run python -m unittest discover -s tests -v
uv build
```

Integration tests require Linux/macOS PTYs and Bash. They start local simulated
Guacamole and vLLM services, launch the actual helper inside a PTY, and test:

- WebSocket and HTTP fallback, including fragmented protocol instructions.
- Model discovery and preservation of upstream HTTP errors.
- Unicode and large prompt bodies beyond the terminal canonical line limit.
- SSE arrival before upstream completion and response flow control.
- Concurrent requests and separate local/upstream API keys.
- Tunnel failure and local consumer cancellation.

Protocol references:

- https://guacamole.apache.org/doc/gug/configuring-guacamole.html#providing-terminal-input-directly-from-javascript
- https://github.com/apache/guacamole-server/blob/main/src/terminal/terminal-handlers.c
- https://github.com/apache/guacamole-server/blob/main/src/terminal/terminal-stdin-stream.c
- https://guacamole.apache.org/doc/gug/protocol-reference.html
