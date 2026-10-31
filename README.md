# 🐸 Guac Bridge

OpenAI-compatible endpoints and SSH through Guacamole.

## Install

Install on both your client computer and the server. Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```shell
uv tool install guac-bridge
uv tool update-shell
```

Each new Guacamole connection checks the server helper's package version.
Different or unreported versions produce a highlighted warning and a connection
attempt. Use `--require-server-version` on either client for strict matching.
Protocol compatibility is always required. Use `--version` on any command to
check its installed version. Restart existing managed bridges to change their
version policy.

The client starts the appropriate server helper automatically. Keep the client running while using the bridge; stop it with **Ctrl+C**.

For the commands below, copy the full URL from your browser after opening the desired Guacamole web terminal connection. Replace `SERVER_URL` and `YOUR_USERNAME` with your connection details. Your Guacamole password is prompted.

Append `--save-password` to either client command to remember the Guacamole
password in your system credential store after successful login. On Windows,
this uses Windows Credential Manager for your current user on this computer.
Both bridges automatically reuse the saved password for the same deployment URL
and username, regardless of the selected Guacamole connection.

| Option | Behavior |
| --- | --- |
| `--save-password` | Save after successful authentication |
| `--forget-password` | Delete the saved credential and exit |
| `--no-keyring` | Ignore saved credentials for this invocation |

`GUAC_PASSWORD` takes precedence over stored credentials. If no password is
available, the client prompts; saving is opt-in. A rejected saved password gets
one replacement prompt and is updated only after the replacement succeeds.
macOS Keychain and available Secret Service/KWallet stores are also supported.
An unavailable store falls back to prompting, without saving to a plaintext file.
These options store only the Guacamole login, not the inner SSH password or an
OpenAI API key.

## Usage: LLM bridge

The server's OpenAI-compatible endpoint should be available at
`http://127.0.0.1:8000`.

On your client computer:

```shell
guac-llm-client --url "https://SERVER_URL" --username YOUR_USERNAME
```

Configure your LLM application with:

| Setting | Value |
| --- | --- |
| OpenAI base URL | `http://127.0.0.1:8000/v1` |
| Chat completions URL | `http://127.0.0.1:8000/v1/chat/completions` |
| Model | A model ID from `/v1/models` |
| API key | The key required by the server endpoint, or a placeholder if none is required |

Requests, streamed responses, and the application's API key are forwarded to
the server endpoint.

Use `--upstream http://127.0.0.1:9000` for a different server-side endpoint
(omit `/v1`), or `--port 18000` for a different local port.

## Usage: SSH bridge

The server's SSH service should be reachable at `127.0.0.1:22`.

On your client computer:

```shell
guac-ssh-client --url "https://SERVER_URL" --username YOUR_USERNAME
```

In another local terminal:

```shell
ssh -p 2222 serveruser@127.0.0.1
```

Use the server's SSH username and its usual password or SSH key. SSH
authentication and host-key verification are handled by your SSH client.
SCP and SFTP work through the same local port.

Use `--target-port` to select a different server-side SSH port and `--port`
to change the local listener.

Start the bridge automatically before SSH in the background:

```shell
guac-ssh-client --ensure-running --url "https://SERVER_URL" --username YOUR_USERNAME --port 2222
```

The command reuses a listener on the selected port or starts a background bridge
and exits once the port is ready. Keep the port dedicated to this bridge:
an existing listener is reused without checking its destination. Background
startup requires a saved password or `GUAC_PASSWORD` and cannot prompt. Save your
Guacamole password once with `--save-password` in a foreground invocation.

For example, in `~/.ssh/config` (replace both usernames):

```sshconfig
Host guac-server
    HostName 127.0.0.1
    Port 2222
    User SERVER_USERNAME

Match originalhost guac-server exec "guac-ssh-client --ensure-running --url https://SERVER_URL --username YOUR_USERNAME --port %p"
Match all
```

Run `ssh guac-server`. The command must be on SSH's PATH; use its absolute path
if needed. `--ready-timeout 60` limits startup waiting. The bridge keeps running
independently of SSH sessions. Stop it and its active SSH connections with:

```shell
guac-ssh-client --stop --port 2222
```

Output goes to the log path printed at startup; customize it with
`--background-log`. On Windows, optionally add `--show-console` to
`--ensure-running` for a visible console with output, password prompting, and
**Ctrl+C** shutdown. An existing bridge keeps its original settings; stop it
before changing console mode. `--stop` controls bridges started with
`--ensure-running`; stop a foreground bridge in its original terminal.

## Other Remarks
Both bridges support `--ca-file company-ca.pem` for a custom certificate authority and `--transport http` to use Guacamole's HTTP tunnel instead of websockets. If the server-side helper is installed outside `~/.local/bin/`, pass its full path with `--server-command`. Run either client with `--help` for all options.

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
