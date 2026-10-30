# 🐸 Guac Bridge

OpenAI-compatible endpoints and SSH through Guacamole.

## Install

Install on both your client computer and the server. Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```shell
uv tool install guac-bridge
uv tool update-shell
```

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

## Other Remarks
Both bridges support `--ca-file company-ca.pem` for a custom certificate authority and `--transport http` to use Guacamole's HTTP tunnel instead of websockets. If the server-side helper is installed outside `~/.local/bin/`, pass its full path with `--server-command`. Run either client with `--help` for all options.

## Contribution

Submit issues and pull requests on
[GitHub](https://github.com/himbeles/guac-bridge).
Include tests for behavior changes.

From a repository checkout, run the test suite in an environment with Bash and
PTY support:

```shell
uv run python -m unittest discover -s tests -v
```

## Architecture

Each client logs into Guacamole and starts its matching server helper inside
the terminal session. Framed, Base64-encoded messages travel through Guacamole's
input and output pipes.

| Bridge | Local listener | Server helper |
| --- | --- | --- |
| LLM | OpenAI-compatible HTTP API | Forwards requests and streamed responses to the server endpoint |
| SSH | TCP port for your SSH client | Relays bytes to the server's SSH service |

Listeners bind to local loopback. Helpers connect to services on the server's
loopback interface, so no additional inbound server port is needed. Per-connection
flow control bounds buffering; disconnecting Guacamole closes active connections.


