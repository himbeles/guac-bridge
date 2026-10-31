# 🐸 Guac Bridge

OpenAI-compatible endpoints and SSH through Guacamole.

Guac Bridge makes a remote OpenAI-compatible API or SSH service available on a
local port through an existing Guacamole web terminal. Use your usual LLM
clients and SSH tools without direct network access to the server.

## Common first-time setup

Install matching versions on your client computer and the server. Requires
Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```shell
uv tool install guac-bridge
uv tool update-shell
```

Open the desired Guacamole web terminal in your browser and copy its full URL.
Replace `SERVER_URL` and `YOUR_USERNAME` below with that URL and your Guacamole
username. The client starts the installed server helper automatically.

Your Guacamole password is prompted. Optionally add `--save-password` on your
first connection to store it in the system credential store, including Windows
Credential Manager. Both bridges reuse saved credentials for the same deployment
and username. `GUAC_PASSWORD` takes precedence.

| Option | Purpose |
| --- | --- |
| `--save-password` | Save after successful login |
| `--forget-password` | Delete the saved credential and exit |
| `--no-keyring` | Ignore stored credentials for this invocation |

Only the Guacamole password is stored; SSH credentials and API keys are separate.
If the credential store is unavailable, the client prompts without saving.

Apply these options to the client commands below when needed:

- `--ca-file company-ca.pem` for your certificate authority.
- `--transport http` to use the HTTP tunnel instead of WebSocket.
- `--server-command PATH` if the matching helper is outside `~/.local/bin/`.

## SSH bridge

### Setup

The server's SSH service must be reachable at `127.0.0.1:22`. Use
`--target-port` for a different server-side port and `--port` for a different
local listener.

Optionally add an alias to your client's `~/.ssh/config`:

```sshconfig
Host guac-server
    HostName 127.0.0.1
    Port 2222
    User SERVER_USERNAME
```

For automatic bridge startup, first save your Guacamole password by running:

```shell
guac-ssh-client --url "https://SERVER_URL" --username YOUR_USERNAME --save-password
```

Once connected, stop it with **Ctrl+C**, then append to `~/.ssh/config`:

```sshconfig
Match originalhost guac-server exec "guac-ssh-client --ensure-running --url https://SERVER_URL --username YOUR_USERNAME --port %p"
Match all
```

The command must be on SSH's PATH; use its absolute path if needed. Automatic
startup requires saved credentials or `GUAC_PASSWORD`. It reuses an existing
listener, so keep the chosen port dedicated to this destination.

Optional tuning: `--window` defaults to 32 and `--frame-size` to 16384 bytes.
See `guac-ssh-client --help` for throughput and buffering tradeoffs.

### Usage

With automatic startup configured, connect with:

```shell
ssh guac-server
```

Otherwise start the bridge manually:

```shell
guac-ssh-client --url "https://SERVER_URL" --username YOUR_USERNAME
```

Then connect from another local terminal:

```shell
ssh -p 2222 SERVER_USERNAME@127.0.0.1
```

SSH handles the server's password/key and host-key verification. SCP and SFTP
also work through the local port.

Stop a foreground bridge with **Ctrl+C**. An automatically started bridge keeps
running after SSH exits; stop it and its active connections with:

```shell
guac-ssh-client --stop --port 2222
```

Background output goes to the log path printed at startup; customize it with
`--background-log`. On Windows, optionally add `--show-console` to the
`--ensure-running` command for a visible console with output and **Ctrl+C**.
Stop and restart an existing bridge to change its settings.

## LLM bridge

### Setup

The server's OpenAI-compatible endpoint should be available at
`http://127.0.0.1:8000`. For a different endpoint, add
`--upstream http://127.0.0.1:9000` to the client command (omit `/v1`).
Use `--port 18000` to change the local listener.

Configure your LLM application with:

| Setting | Value |
| --- | --- |
| OpenAI base URL | `http://127.0.0.1:8000/v1` |
| Chat completions URL | `http://127.0.0.1:8000/v1/chat/completions` |
| Model | A model ID from `/v1/models` |
| API key | The server endpoint's key, or a placeholder if none is required |

Adjust the application's local port if you change `--port`.
Optional `--window` tuning defaults to 32; see `guac-llm-client --help`.

### Usage

Start the bridge on your client computer:

```shell
guac-llm-client --url "https://SERVER_URL" --username YOUR_USERNAME
```

Use your configured LLM application. Requests, streamed responses and its API
key are forwarded to the server endpoint. Keep the bridge running while using
it; stop with **Ctrl+C**.

## Contribution

Submit issues and pull requests on
[GitHub](https://github.com/himbeles/guac-bridge).
Include tests for behavior changes. See [Testing connections](docs/testing.md)
for the test architecture, coverage, and commands.

From a repository checkout, run the test suite in an environment with Bash and
PTY support:

```shell
uv run python -m unittest discover -s tests -v
```

## Architecture

Each client logs into Guacamole and starts its matching helper inside the
server's SSH terminal. The SSH bridge relays TCP connections; the LLM bridge
forwards HTTP API requests and streamed responses. Both use local loopback
listeners and require no additional externally accessible server port.

See [Connection details](docs/connections.md) for flowcharts, the two SSH
connections, loopback addresses and ports, authentication, and bridge lifetime.
See [Benchmarking](docs/benchmarking.md) for live transfer tests, LLM timing
comparisons, and controlled transport benchmarks.
