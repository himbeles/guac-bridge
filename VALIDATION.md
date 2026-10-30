Validation for version 0.2.0: 2026-10-30

- Linux, Python 3.12.14, aiohttp 3.14.4.
- Existing HTTP relay regression tests retained.
- SSH TCP relay integration uses the actual helper in a Bash PTY through a
  simulated Guacamole gateway. It covers binary integrity, WebSocket and HTTP
  fallback, concurrency, half-close in both directions, connection limits,
  refused target connections, and tunnel loss.
- Real OpenSSH execution/SCP/forwarding test is opt-in. This execution environment
  prohibits the chroot needed by the OpenSSH privilege-separation sandbox, so
  this check could not run here. No production Guacamole/SSH session was tested.
- No corporate URLs or login names are included in source or documentation.
