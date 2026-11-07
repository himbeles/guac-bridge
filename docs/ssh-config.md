# SSH config integration

```sshconfig
Host guac-server
    HostName 127.0.0.1
    Port 2222
    User SERVER_USERNAME

Match originalhost guac-server exec "guac-bridge ssh CONNECTION_URL --save-password --user GATEWAY_USERNAME --port %p"
Match all
```

Connect with `ssh guac-server`. The hook starts or reuses the bridge and relays
startup progress. The bridge stays running after SSH exits. Each SSH alias can use
a different local port while retaining remote port 22.

