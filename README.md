# 🐸 Guac Bridge

SSH and OpenAI-compatible endpoints through Guacamole.

Expose a remote server’s SSH service or OpenAI-compatible API on your computer through its Guacamole web terminal. Use your usual SSH tools or LLM applications with the local port.

## First-time setup

On **Windows x86-64**, run the installer from [Releases](https://github.com/himbeles/guac-bridge/releases). Select **Add Guac Bridge to my user PATH** if desired. On **Linux x86-64**, extract the native release archive.

Alternatively, install on either client with uv:

```shell
uv tool install guac-bridge
uv tool update-shell
```

No installation is needed on the server. The client uploads its bundled Linux x86-64 helper through the Guacamole terminal and reuses it on later connections. The server needs a normal shell and core utilities, with an executable user cache directory; no runtime installation, root access or internet connection is required there.

Open the desired Guacamole terminal in your browser and copy its full URL. Use it as `CONNECTION_URL` below.

## SSH bridge

### Connect

```shell
guac-bridge ssh --url CONNECTION_URL
```

Enter your Guacamole username and password. In another terminal:

```shell
ssh -p 2222 SERVER_USERNAME@127.0.0.1
```

Keep the bridge running while connected. Stop it with **Ctrl+C**.

### Use an SSH config

Add to `~/.ssh/config`, replacing the URL and both usernames:

```sshconfig
Host guac-server
    HostName 127.0.0.1
    Port 2222
    User SERVER_USERNAME

Match originalhost guac-server exec "guac-bridge ssh --ensure-running --save-password --url CONNECTION_URL --username GUACAMOLE_USERNAME --port %p"
Match all
```

Connect with `ssh guac-server`. The hook starts or reuses the background bridge and shows startup progress in your calling console. On first login it prompts in the calling console and saves your password to the system credential store after successful authentication.

The bridge stays running after SSH exits. Check or stop it with:

```shell
guac-bridge ssh --status --port 2222
guac-bridge ssh --stop --port 2222
```

Use a dedicated local port for each destination. Windows users can add `--show-console` to see the background bridge in a separate console.

## LLM bridge

```shell
guac-bridge llm --url CONNECTION_URL
```

Enter your Guacamole credentials. The default local port is **8000**, forwarding the server’s HTTP endpoint at **127.0.0.1:8000**, including streamed responses. Use `--target-port` for a different server port and `--port` for a different local port.

Keep the bridge running while using the application. Stop it with **Ctrl+C**.

The commands `guac-ssh-client` and `guac-llm-client` also work. Run any client with `--help` for credential storage, certificates, background operation and transport tuning.

## Contribution

The native CLI, helper and shared transport live in one [Cargo package](crates/guac-bridge). The uv package installs the same native executables.

See [Building](docs/building.md), [Testing](docs/testing.md) and [Benchmarking](docs/benchmarking.md). Submit issues and pull requests on [GitHub](https://github.com/himbeles/guac-bridge).

## Architecture

Each bridge opens a Guacamole WebSocket connection, checks or uploads its bundled helper, and runs it inside the remote terminal. Both bridge types forward TCP streams with bounded flow control; HTTP headers, bodies and streaming responses pass through unchanged. The helper exits with the session; only its cached executable remains.

See [Connection details](docs/connections.md) for diagrams, loopback interfaces and process lifetimes.
