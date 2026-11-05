# FortiGate web-SSH integration assessment

This assessment uses two supplied browser HARs and their embedded portal JavaScript: an authenticated bookmark session and a portal-login/custom-SSH session.
The v5 beta implements the custom-SSH/password-login/Windows-carrier subset below.
Simulator coverage exercises the real helper, binary forwarding, streaming HTTP,
authentication failures, private background handoff and gateway labels. A deployed
FortiGate and actual remote Windows console remain unvalidated. Sections below
also describe planned bookmark and broader authentication support.
No gateway hostnames, bookmark names, usernames, passwords or session values from
that capture are reproduced here.

## Confirmed protocol

The deployment is a FortiGate SSL-VPN web portal. It is not a native FortiClient
VPN tunnel and does not use the Guacamole wire protocol.

The first capture contains 34 requests and 35 WebSocket messages and begins inside
an already authenticated portal. The second contains 36 requests and 11 WebSocket
messages and includes a successful portal password login and custom SSH startup.

1. The browser loads `/sslvpn/portal/index.html`.
2. `GET /remote/portal` supplies portal/session metadata, including realm,
   session identifiers, CSRF metadata and timeout settings.
3. `GET /remote/portal?access=admin` supplies portal configuration and configured
   bookmark groups; `GET /remote/portal/bookmarks` supplies user bookmarks.
4. An SSH bookmark opens
   `wss://GATEWAY/remote/rds?connection=URL_ENCODED_JSON`, negotiating the `hterm`
   WebSocket subprotocol. The JSON has `name`, `apptype`, `host`, `port`,
   `bookmark-type` and `bookmark-group`. Preserve server-supplied bookmark identity;
   do not infer that arbitrary destinations are permitted.
5. The browser sends length-prefixed text instructions in this order:
   `resize` (columns:rows), `user`, `passwd`, then `start` with an empty argument.
   Subsequent keyboard input uses `str` instructions.
6. Received messages in this recording are raw terminal text with ANSI escapes,
   including PowerShell/Windows console output. They are not Guacamole `pipe`,
   `blob`, `ack` or `sync` instructions.

The embedded JavaScript confirms these framing rules:

```text
6.resize,L.COLUMNS:ROWS;
4.user,L.USERNAME;
6.passwd,L.PASSWORD;
5.start,0.;
3.str,L.INPUT;
```

Each L is the corresponding argument length. The browser uses JavaScript string
length (UTF-16 code units), which must be reproduced for non-ASCII values rather
than assuming Rust UTF-8 byte length or Unicode scalar count. The addon also has
a binary-WebSocket input path, but this capture does not exercise it. Do not
assume that arbitrary binary data survives the complete gateway/console path.

The JavaScript exposes `GET /remote/portal?access=keepalive`; the capture itself
shows portal status polling with `action=1`, not a demonstrated keepalive exchange.
Neither HAR contains Cookie/Set-Cookie headers or populated cookie arrays. The
second establishes the successful password-login request/redirect sequence, but
does not identify session-cookie names, attributes or issuance. Cookie persistence
and renewal still require implementation validation.

## Portal login and custom SSH: second capture

The second HAR records `POST /remote/logincheck` with URL-encoded text containing
`ajax=1`, `username` and `credential`. Its recorded Content-Type is
`text/plain;charset=UTF-8`; do not silently assume the browser sent the ordinary
form Content-Type. The HTTP 200 body is a Fortinet key/value response, with
`ret=1,redir=...` followed by user/group/portal metadata. Parse the protocol result
and redirect rather than treating any HTTP 200 as authenticated success.

The redirect goes to `/remote/hostcheck_install` with authentication and portal
metadata. In this session that response is a small HTML/JavaScript navigation to
`/sslvpn/portal/index.html`. This capture does not show an endpoint-compliance
challenge; it does not establish that other accounts/deployments lack one. Follow
the supported flow and fail clearly on unsupported challenges rather than skip it.

The client should retain server-issued session cookies in memory across the login,
redirects, portal requests and the WSS handshake. Preserve their origin/path/secure
scope, and require authenticated portal access before opening the terminal. The
HAR has omitted cookie material, so the exact cookie name and issuance remain
unknown. Cookie names and attributes from Edge's Cookies pane would resolve that
without collecting secret values. No MFA or SSO exchange appears in this successful
password-login capture; support must not be inferred for unobserved flows.

For the custom SSH session, `connection` contains only `name`, `apptype`, `host`
and `port`; `apptype` is `ssh` and `port` is a string. Bookmark identity fields are
absent. Reproduce this variant for custom connections rather than fabricating
bookmark groups. The same `hterm` resize/user/passwd/start handshake follows, and
raw PowerShell terminal output is returned. This confirms separate portal and
remote SSH authentication in both session types.

A custom-session CLI could use:

```sh
guac-bridge ssh https://GATEWAY --gateway fortigate --server-host HOST --server-port 22 --server-user USER --server-os windows
```

These SSH target options identify the terminal opened by FortiGate. They are distinct
from the existing `--target-host`/`--remote-port`, which identify the TCP service
opened by the helper inside that terminal, and from the local `--port`. Keep portal
and SSH credentials distinct in prompts, credential-store identities and the private
managed-child handoff. The existing `--user` can continue to mean gateway login.

## Reuse boundaries

Keep the local TCP listener, channel IDs and credits, TCP half-closes, byte-stream
HTTP forwarding, authenticated local management, update exclusion, tray, process
isolation and packaging. Retain the shared helper framing and reliable-input
protocol wherever the new terminal carrier can preserve them.

Extract a gateway-session boundary from `client.rs` and `client/guacamole.rs`.
A session should provide bounded input submission, terminal/bootstrap output,
terminal readiness/failure and cancellation, while owning gateway authentication,
WebSocket framing, TLS/proxy use and session renewal. An enum with concrete
Guacamole and FortiGate implementations is sufficient; a plugin framework is not
needed.

Guacamole must retain its current named streams, sync, blob acknowledgement and
flow-control behavior inside its adapter. FortiGate wraps text input as `str` and
consumes raw text/binary output according to its own protocol. No Guacamole
`sync` wait or acknowledgement-slot timeout should run on a FortiGate session.
Gateway acknowledgement, socket-write completion, helper upload receipt and
helper protocol acknowledgement remain distinct concepts.

The generic forwarding layer should consume decoded helper frames and submit
helper input through the selected session/carrier. Preserve remote-consumption
receipts and reliable-input retries; a completed WebSocket write does not prove
that the shell or helper consumed the input.

## Carrier and deployment

The recorded target is Windows/PowerShell. Start with that supported path rather
than assuming a Linux raw-terminal carrier. Reuse PowerShell upload/checksum/cache
logic and the existing printable input/OSC-output helper carrier as candidates.
Guacamole currently delivers OSC data through named pipe instructions; FortiGate
needs an incremental raw-terminal OSC scanner to extract those payloads directly.
Bootstrap receipts similarly need to be detected outside the Guacamole BOOT pipe.
Handle markers split across messages, unrelated ANSI text, shell echo and bounded
malformed/incomplete envelopes.

First prove that this gateway preserves the existing OSC envelopes and encoded
input end to end. If it strips or transforms them, choose an explicit carrier
variant instead of changing the shared frame protocol or weakening checksums.
Only after that proof can the current Windows helper be claimed reusable without
changes. Linux over FortiGate requires its own raw-byte or printable-carrier test.

## Authentication and identity

Portal authentication and target SSH authentication are separate. The WebSocket
`user`/`passwd` messages authenticate the SSH terminal. The second capture separately
establishes `/remote/logincheck` portal password login. FortiGate must not reuse
Guacamole's `/api/tokens` login.

Implement the observed password-login path first, with explicit errors for
unrecognized MFA/SSO or endpoint-compliance responses.
Alternatively, a first proof of concept can accept an explicitly provided active
portal session through a private local input channel. Do not embed the captured
session in code, command arguments, registrations or test fixtures. Interactive
portal/MFA acquisition belongs in the foreground launcher before the managed child
starts. Extend the private startup handoff to structured, provider-specific session
material as needed; preserve the current no-secrets-in-argv property.

Use a separate remote SSH credential prompt/store entry unless the bookmark's
supported SSO behavior is established. Keep session cookies and CSRF values scoped
to the originating gateway. Reuse native CA trust, custom CA configuration and the
existing proxy connection code for both HTTP and WSS.

A proposed CLI is:

```sh
guac-bridge ssh https://GATEWAY --gateway fortigate --bookmark BOOKMARK --server-os windows
```

Keep `ssh` and `http` as the local service modes. Add gateway type and stable bookmark
identity to bridge reuse hashing, credential namespaces and route display. Pass
those options through managed startup. Keep remote OS/carrier selection independent
of the client's OS and gateway vendor.

## Implementation and validation sequence

1. Build sanitized protocol fixtures from the observed shapes, not captured secrets.
   Test connection JSON encoding, UTF-16 lengths, `hterm` negotiation, initialization
   order, text input and raw-terminal output.
2. Extract the session boundary with Guacamole behavior unchanged. Run its existing
   native, Windows, half-close, streaming, upload and process-lifecycle suites.
3. Add the FortiGate adapter and a simulated FortiGate gateway with separate portal
   and SSH authentication, bookmark selection, session expiry and bounded failures.
4. Validate the existing Windows helper carrier and PowerShell upload against a real
   authorized bookmark: marker pass-through, all byte values in both directions,
   frame/message fragmentation, upload checksum/cache repair, concurrency, both
   half-closes and streamed HTTP. Treat this as a prerequisite to readiness.
5. Implement the observed portal password login, scoped cookie handling and renewal;
   validate them live and cover rejected/expired sessions,
   MFA cancellation and target-SSH authentication failure separately.
6. Verify managed startup/reuse/status/stop, tray display, update exclusion and
   provider-specific identity. Add Linux-server support only after its carrier is
   independently validated.

The captures establish the password-login and custom/bookmarked terminal-adapter
protocol direction. Session-cookie issuance, renewal behavior and helper-carrier
transparency still require verification. No connection
to the captured gateway or replay of captured credentials was performed.
