# 🐸 Guac Bridge

OpenAI-compatible endpoints and SSH through Guacamole.

Access a remote server’s SSH service or OpenAI-compatible API through its Guacamole web terminal. Guac Bridge exposes the service on a local port for your usual SSH tools or LLM applications.

## First-time setup

Install matching versions on **the server**, using its Guacamole terminal, and on **your client computer**:

```shell
uv tool install guac-bridge
uv tool update-shell
```

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). The client starts the server helper automatically.

Open the desired Guacamole terminal in your browser and copy its full URL. Use it as `CONNECTION_URL` below.

## SSH bridge

### Connect

On your client:

```shell
guac-ssh-client --url "CONNECTION_URL"
```

Enter your Guacamole username and password when prompted. In another terminal:

```shell
ssh -p 2222 SERVER_USERNAME@127.0.0.1
```

Keep the bridge running while connected. Stop it with **Ctrl+C**.

### Use an SSH config

First, save your Guacamole login so the hook can connect without prompting:

```shell
guac-ssh-client --url "CONNECTION_URL" --username GUACAMOLE_USERNAME --save-password
```

Once connected, stop this foreground bridge with **Ctrl+C**. Add the following
to `~/.ssh/config`, replacing the URL and both usernames:

```sshconfig
Host guac-server
    HostName 127.0.0.1
    Port 2222
    User SERVER_USERNAME

Match originalhost guac-server exec "guac-ssh-client --ensure-running --url \"CONNECTION_URL\" --username GUACAMOLE_USERNAME --port %p"
Match all
```

Connect with:

```shell
ssh guac-server
```

The `Match` hook starts or reuses the background bridge automatically.
Use a dedicated local port for each destination.

The bridge stays running after SSH exits. Stop it and its active connections with:

```shell
guac-ssh-client --stop --port 2222
```

## LLM bridge

### Connect

On your client:

```shell
guac-llm-client --url "CONNECTION_URL"
```

Enter your Guacamole username and password when prompted. By default, the bridge forwards the server’s endpoint at `http://127.0.0.1:8000`.

Keep the bridge running while using the application. Stop it with **Ctrl+C**.

## Other options

Run either client with `--help` for credential storage, certificates, custom ports, transport tuning and opt-in server updates.

## Contribution

Submit issues and pull requests on [GitHub](https://github.com/himbeles/guac-bridge).

Run tests from a checkout with Bash and PTY support:

```shell
uv run python -m unittest discover -s tests -v
```

See [Testing connections](docs/testing.md) for the test architecture and coverage.

## Architecture

Each bridge opens a Guacamole connection and starts its matching helper inside the remote terminal. The SSH bridge forwards TCP connections; the LLM bridge forwards HTTP requests and streamed responses. Neither requires an additional externally accessible server port.

See [Connection details](docs/connections.md) for diagrams, authentication and bridge lifetime, and [Benchmarking](docs/benchmarking.md) for performance measurements.
