# Guacamole LLM Bridge

One Python package with four installed commands:

| Command | Machine | Purpose |
| --- | --- | --- |
| `guac-llm-client` | Windows, macOS, or Linux client PC | Log into Guacamole and expose an OpenAI-compatible HTTP endpoint on local loopback |
| `guac-llm-server` | Linux server running vLLM | Relay HTTP through the Guacamole SSH terminal using stdin/stdout |
| `guac-ssh-client` | Windows, macOS, or Linux client PC | Expose the remote SSH service on local loopback |
| `guac-ssh-server` | Linux server | Relay binary TCP data to a server-side loopback SSH service |

Requires Python **3.11 or newer** and `uv`. The server helper uses only the Python
standard library; the shared package installs `aiohttp` for the client command.

This is an initial implementation. Automated integration tests use a simulated
Guacamole gateway and the actual helper in a Linux PTY. It has **not been tested
against a production Guacamole deployment**.

## Installation

Install on both machines from PyPI:

```shell
uv tool install guac-llm-bridge
uv tool update-shell
```

Open a fresh terminal if required by `update-shell`. All four commands are installed
on both machines; use the appropriate one on each. Upgrade an existing PyPI
installation with `uv tool upgrade guac-llm-bridge`.

To install from source instead:

```shell
git clone https://github.com/himbeles/guac-llm-bridge.git
cd guac-llm-bridge
uv tool install .
```

If needed, select a suitable Python with `uv tool install --python 3.11 guac-llm-bridge`.

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

## SSH, SCP, SFTP, and port forwarding

Version 0.2.0 adds an SSH TCP relay. Install or upgrade the same package on both
machines:

```shell
uv tool install --upgrade guac-llm-bridge
```

On the Linux server, verify `~/.local/bin/guac-ssh-server --help` works. Keep the
normal SSH daemon running and reachable from that server at `127.0.0.1:22`.
**Do not start the relay helper manually**; the client starts it automatically.
It intentionally requires the internal `--stdio` flag to prevent accidental
interactive startup.

On the client PC, keep this command running:

```shell
guac-ssh-client --url "https://guacamole.example.com/guacamole/#/client/CLIENT_ID" --username YOUR_GUACAMOLE_USERNAME
```

Copy the full connection URL from the Guacamole browser session. The client
logs into Guacamole, starts `~/.local/bin/guac-ssh-server` in that terminal, and
listens on `127.0.0.1:2222`. Use `--server-command /absolute/path/guac-ssh-server`
if the remote executable is installed elsewhere.

In a second local terminal, use your normal SSH tools:

```shell
ssh -p 2222 linuxuser@127.0.0.1
scp -P 2222 file.txt linuxuser@127.0.0.1:~/
sftp -P 2222 linuxuser@127.0.0.1
```

The inner SSH connection performs its own authentication and host-key
verification. Use the Linux server's SSH username, password, or private key;
the Guacamole login does not authenticate this second connection. Verify the
host-key fingerprint against the destination server as you normally would.

For convenient use, add this to your local `~/.ssh/config` (on Windows,
`%USERPROFILE%\.ssh\config`):

```sshconfig
Host server-via-guacamole
    HostName 127.0.0.1
    Port 2222
    User linuxuser
    HostKeyAlias my-linux-server
    ServerAliveInterval 15
    ServerAliveCountMax 3
```

After starting the bridge, you can use `ssh server-via-guacamole` and
`scp file.txt server-via-guacamole:~/`. The same host entry can be selected in
VS Code Remote SSH; that integration has not been tested here.

To expose vLLM through ordinary SSH forwarding:

```shell
ssh -N -L 8000:127.0.0.1:8000 server-via-guacamole
```

The local OpenAI base URL is then `http://127.0.0.1:8000/v1`. This uses the SSH
relay instead of `guac-llm-client`. SSH port forwarding must be allowed by the
server's sshd configuration. A typical server configuration requires no bridge
changes.

Useful relay options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--port` | `2222` | Client PC listener, always bound to `127.0.0.1` |
| `--target-host` | `127.0.0.1` | Server-side target; only loopback hosts are accepted |
| `--target-port` | `22` | Server-side SSH service port |
| `--max-connections` | `8` | Simultaneous TCP connections; at most 16 |
| `--server-command` | `~/.local/bin/guac-ssh-server` | Installed remote helper path |
| `--transport` | `auto` | WebSocket with HTTP tunnel fallback |
| `--startup-delay` | `2` | Seconds to allow remote shell startup |
| `--connect-timeout` | `30` | Connection, helper startup, and pipe acknowledgment timeout |
| `--ca-file`, `--insecure` | System trust | Same Guacamole TLS options as the HTTP client |
| `--connection-id`, `--data-source` | From URL | Override Guacamole connection selection |

Guacamole credentials can be prompted or supplied using `--username` and
`GUAC_PASSWORD`. `VLLM_API_KEY` and `BRIDGE_API_KEY` apply only to the HTTP relay;
SSH authentication is handled by the SSH client itself.

Binary TCP data is Base64-framed through the terminal pipes, with bounded
per-connection flow control in both directions. Half-close is preserved, so a
peer can finish sending and still receive data. Losing Guacamole aborts active
TCP connections; existing SSH sessions cannot resume after reconnecting. The
additional framing and terminal transport reduce throughput compared with a
direct SSH connection. The helper is a terminal process, not an SSH daemon.

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

## Publishing releases

The workflow `.github/workflows/publish.yaml` uses PyPI Trusted Publishing
with GitHub environment `pypi`; no PyPI API token is needed. The publisher must
be registered on PyPI for owner `himbeles`, repository `guac-llm-bridge`,
workflow `publish.yaml`, and environment `pypi`.

Publishing runs when `pyproject.toml` changes on `main`, when a `v*` tag is
pushed, or from **Actions → Publish to PyPI → Run workflow**. For each new release,
increment `project.version` in `pyproject.toml` and `__version__` in
`src/guac_llm_bridge/__init__.py`, then push to `main`. A release tag, if used,
must match that version, for example `v0.1.0`.

Tests on Python 3.11 and 3.14 must pass before the distributions are built and
validated. A separate job downloads those distributions and publishes with
OIDC credentials scoped to the `pypi` environment. Each version can be uploaded
only once; rerunning a successful publication with the same version will fail.


SSH relay tests additionally cover binary transfers beyond the flow-control
window, concurrent connections, connection limits, independent close handling,
TCP half-close in both directions, refused upstream connections, and tunnel loss.
An optional real OpenSSH test exercises remote execution, SCP via SFTP, and local
port forwarding. It requires `GUAC_TEST_OPENSSH=1`, root, sshd, and an existing
`/run/sshd`; it skips when the environment prohibits OpenSSH's sandbox. No
production SSH server or VS Code Remote SSH session has been validated.
