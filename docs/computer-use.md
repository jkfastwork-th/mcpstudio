# Computer Use / Web VNC

## What this is

A shared browser desktop surfaced inside MCP Studio for OAuth approval, login
consent screens, and any human-in-the-loop action that needs a real browser
profile. Studio brokers the path from the web app to a local websockify bridge;
it does not start the VNC server or the bridge itself.

## What this is not

- Not a VNC server.
- Not a websockify launcher.
- Not a remote access gateway. The only inbound path is the authenticated Studio
  websocket route; raw VNC host/port is never exposed to remote clients.
- Not a credential store. Passwords, OTPs, and the computer auth token are never
  persisted by Studio.

## Prerequisites

1. A VNC server on the loopback host/port described in config (`computer_vnc_host`,
   `computer_vnc_port`).
2. A websockify bridge listening on the loopback host/port (`computer_websockify_host`,
   `computer_websockify_port`) and forwarding to that VNC server.
3. The noVNC directory present on the host if you want the local noVNC iframe.
   If it is missing, the Computer panel still loads but the connect button is
   disabled and the panel explains the missing dependency. No public CDN is used.

### Minimal working local stack

A common loopback setup looks like this:

```bash
# VNC desktop on :2 (5902). Run once per host boot, as the same user that
# launches Studio, so cookies/SSL state live in a real browser profile.
Xvfb :2 -screen 0 1280x720x24 -ac &
export DISPLAY=:2
# login manager / browser profile start here, e.g. firefox --profile … &
/usr/bin/tigervncserver :2 -geometry 1280x720 -depth 24

# Bridge loopback 6080 -> VNC 5902
websockify --web=/usr/share/novnc 6080 localhost:5902
```

With that running and `computer_use_enabled=true`, open
`http://<studio-host>:<bind_port>/computer` in the Studio web app. Choose a
managed session, enter the computer auth token if one is configured, and connect.
The noVNC iframe opens a websocket to `/api/computer/vnc/ws/<session-id>`, which
Studio proxies to `ws://127.0.0.1:6080`.

## Config fields (`studio.`)

| Field | Default | Purpose |
|---|---|---|
| `computer_use_enabled` | `false` | Master switch. When false, all computer routes return disabled status and reject connections. |
| `computer_vnc_host` | `127.0.0.1` | VNC server host. Must be loopback when enabled. |
| `computer_vnc_port` | `5902` | VNC server port. |
| `computer_websockify_host` | `127.0.0.1` | websockify bridge host. Must be loopback when enabled. |
| `computer_websockify_port` | `6080` | websockify bridge port. |
| `computer_cdp_port` | `9222` | CDP/Chrome DevTools port reserved for browser automation candidates. Not wired yet. |
| `computer_novnc_dir` | `/usr/share/novnc` | Local noVNC directory. If it contains `vnc.html`, Studio mounts it at `/computer/novnc`. |
| `computer_auth_token` | `null` | Optional bearer/token guard for the websocket route. If set, connections must present it via `Authorization: Bearer <token>` or `?token=<token>`. |

Config is loaded from `config.yaml` and overridden by environment variables using
the existing `M6_STUDIO_*` / `M6_*` prefix convention. See `config.example.yaml`.

## Security model

- Loopback only. When `computer_use_enabled=true`, both `computer_vnc_host` and
  `computer_websockify_host` are validated to be loopback. Non-loopback values
  are rejected at startup.
- Authenticated entry. The only path into a VNC session is
  `WS /api/computer/vnc/ws/{managed_session_id}`. That route validates that
  computer use is enabled, that the managed session exists in the database, and
  (if `computer_auth_token` is set) that the client presented the token.
- No raw port exposure. `GET /api/computer/descriptor/{managed_session_id}` returns
  the Studio viewer URL and the Studio websocket path. It does not return the VNC
  host or port.
- No credentials persisted. The auth token is checked at connect time and never
  stored in the browser or in the database.

## Sharing the session

The desktop is intentionally shared. A human opens it in the Studio web app to
complete a login/OAuth flow. Once the browser profile has the session cookie, an
agent that later uses the same managed session can continue from that authenticated
state. This is the same pattern as a normal workstation: one desktop, multiple
actors, one cookie jar.

Agent and human activity share the same VNC display. Do not treat it as isolated
per-agent. If isolation is required, run separate VNC/websockify stacks per agent
and expose them through separate managed sessions — that is future work.

## Troubleshooting

- **Connect disabled, “Local noVNC assets are missing”**: install noVNC on the
  host and point `computer_novnc_dir` at it. Studio does not fall back to a CDN.
- **Connect disabled, “Local websockify bridge is offline”**: confirm the
  websockify process is running on `computer_websockify_host:computer_websockify_port`.
- **401 on websocket**: if `computer_auth_token` is set, include it as a Bearer
  header or `?token=` query param on the websocket path.
- **404 on websocket/descriptor**: the managed session id does not exist in the
  database or has been removed.
- **VNC viewer shows a black screen**: confirm the VNC server is running on the
  configured host/port and that websockify is bridging to it.

## Development notes

- The websocket proxy forwards binary and text frames bidirectionally between the
  browser and the local websockify. It does not interpret the VNC protocol.
- The descriptor endpoint is the only place that builds the viewer URL and the
  Studio websocket path. The UI should always read those from the descriptor rather
  than hardcoding them.
- `computer_cdp_port` is reserved. CDP-based automation is not part of this MVP.
