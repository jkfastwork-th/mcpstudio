# Computer Use / Web VNC — operational doc

## What this is

A **shared loopback browser desktop** surfaced inside the Studio web app. It
is used for OAuth approval, login/consent screens, and human-in-the-loop
actions that need a real browser. Studio **does not** start VNC or websockify
here — that must already be running on the host.

## What this is not

- It is not a VNC server.
- It is not a websockify launcher.
- It is not a full "computer control" API.
- It does not persist the auth token or any password in the browser.

## Prerequisites

1. A VNC server on the loopback VNC host/port in config.
2. A websockify bridge listening on the loopback websockify host/port in
   config and forwarding to that VNC server.
3. The noVNC directory present on the host if you want the local noVNC
   iframe. If it is missing, the Computer view still loads but the connect
   path is disabled.

## Config fields (`studio.`)

| Field | Default | Purpose |
|---|---|---|
| `computer_use_enabled` | `false` | Master switch. |
| `computer_vnc_host` | `127.0.0.1` | Underlying VNC desktop host. |
| `computer_vnc_port` | `5902` | Underlying VNC desktop port. |
| `computer_websockify_host` | `127.0.0.1` | Websockify bridge host. |
| `computer_websockify_port` | `6080` | Websockify bridge port. |
| `computer_cdp_port` | `9222` | Browser automation/debug port hint used by the status surface. |
| `computer_novnc_dir` | `/usr/share/novnc` | Path to a local noVNC checkout. |
| `computer_auth_token` | `null` | Optional shared secret for the Computer view. |

## Fail-closed rules

- Both `computer_vnc_host` and `computer_websockify_host` must be loopback
  only. `localhost` nicknames are rejected in config validation; only
  `127.0.0.1` and `::1` are accepted.
- All three ports must be in `[1024, 65535]`.
- If `computer_use_enabled` is true and any loopback check fails, startup
  refuses to load the config.
- The descriptor returned to the browser UI must never expose raw VNC host,
  raw VNC port, or the auth token.

## Auth token

When `computer_auth_token` is set, the WebSocket route requires it. It can be
sent as `Authorization: Bearer <token>` or as `?token=<token>` on the
WebSocket URL. The token is never stored by the browser UI.

## noVNC mount

- Only mounted when `computer_novnc_dir` points to a real directory that
  contains `vnc.html`.
- Mounted at `/computer/novnc`.
- No CDN is used.

## API surface

- `GET /api/computer/status` — status snapshot. Never exposes secrets.
- `GET /api/computer/descriptor/{managed_session_id}` — viewer descriptor for
  a managed session. Validates the session, never leaks VNC host/port/token.
- `GET /computer` — redirects to `/#computer`.
- `WS /api/computer/vnc/ws/{managed_session_id}` — authenticated proxy to the
  local websockify bridge. Loopback-only.

## UI connect flow

1. Select a managed session.
2. Optionally enter the computer token.
3. Connect opens the local noVNC iframe with `autoconnect=true`,
   `resize=scale`, and `path=api/computer/vnc/ws/{session_id}`.
4. Disconnect sets the iframe to `about:blank`.
5. Reconnect repeats the Connect flow.
6. Fullscreen uses the browser fullscreen API on the viewer shell.

## Security posture

- The only path into the desktop is the Studio WebSocket route.
- That route validates the managed session and optional bearer token.
- The raw VNC port is never exposed to remote clients.
- The browser UI never stores the token or password.
