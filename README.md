# Guacamole LLM Bridge

Access an OpenAI-compatible endpoint or SSH service on a server through its
Guacamole web terminal.

## Install

Install on both your client computer and the server. Requires Python 3.11+ and
[uv](https://docs.astral.sh/uv/).

```shell
uv tool install guac-llm-bridge
uv tool update-shell
```

The client starts the appropriate server helper automatically. Keep the client
running while using the bridge; stop it with **Ctrl+C**.

For the commands below, copy the full URL from your browser after opening the
desired Guacamole connection. Replace `CLIENT_ID` and `YOUR_USERNAME` with
your connection details. Your Guacamole password is prompted.

## Usage: LLM bridge

The server's OpenAI-compatible endpoint should be available at
`http://127.0.0.1:8000`.

On your client computer:

```shell
guac-llm-client --url "https://guacamole.example.com/#/client/CLIENT_ID" --username YOUR_USERNAME
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
guac-ssh-client --url "https://guacamole.example.com/#/client/CLIENT_ID" --username YOUR_USERNAME
```

In another local terminal:

```shell
ssh -p 2222 serveruser@127.0.0.1
```

Use the server's SSH username and its usual password or SSH key. SSH
authentication and host-key verification are handled by your SSH client.
SCP and SFTP work through the same local port.

To forward the server's OpenAI-compatible endpoint through SSH:

```shell
ssh -p 2222 -N -L 8000:127.0.0.1:8000 serveruser@127.0.0.1
```

Use `--target-port` to select a different server-side SSH port and `--port`
to change the local listener.

Both bridges support `--ca-file company-ca.pem` for a custom certificate
authority and `--transport http` to use Guacamole's HTTP tunnel.
If a helper is installed outside `~/.local/bin/`, pass its full path with
`--server-command`. Run either client with `--help` for all options.

## Contribution

Submit issues and pull requests on
[GitHub](https://github.com/himbeles/guac-llm-bridge).
Include tests for behavior changes.

From a repository checkout, run the test suite in an environment with Bash and
PTY support:

```shell
uv run python -m unittest discover -s tests -v
```
