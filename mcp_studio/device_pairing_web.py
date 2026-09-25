from __future__ import annotations

import html
from typing import Any


def render_pairing_page(
    session: dict[str, Any],
    *,
    nonce: str = "",
    owner_session_valid: bool = False,
    error: str | None = None,
) -> str:
    session_id = html.escape(str(session.get("session_id") or ""), quote=True)
    name = html.escape(str(session.get("name") or "HIRDA Device"))
    platform_name = html.escape(str(session.get("platform") or "unknown"))
    architecture = html.escape(str(session.get("architecture") or "unknown"))
    state = str(session.get("state") or "unknown")
    capabilities = ", ".join(
        html.escape(str(item))
        for item in (session.get("capabilities") or [])
    ) or "none"
    error_html = (
        f'<p class="error">{html.escape(error)}</p>' if error else ""
    )

    if state == "pairing":
        nonce_value = html.escape(str(nonce or ""), quote=True)
        body = f"""
        <p>Confirm the 8-digit code shown on <strong>{name}</strong>.</p>
        <form method="post" action="/pair/{session_id}/verify">
          <input type="hidden" name="nonce" value="{nonce_value}">
          <input name="code" inputmode="numeric" autocomplete="one-time-code"
                 pattern="[0-9 ]{{8,9}}" maxlength="9"
                 placeholder="4827 1936" required autofocus>
          <button type="submit">Verify device</button>
        </form>
        """
    elif state == "pending_owner":
        owner_field = "" if owner_session_valid else """
          <label>Owner approval token</label>
          <input type="password" name="owner_token" autocomplete="off" required>
        """
        body = f"""
        <p>The code matched. Review this device before granting trust.</p>
        <p><strong>Requested capabilities:</strong> {capabilities}</p>
        <form method="post" action="/pair/{session_id}/approve">
          {owner_field}
          <button type="submit">Approve device</button>
        </form>
        """
    elif state == "trusted":
        body = """
        <p><strong>Device trusted.</strong></p>
        <p>You can return to the device. Its outbound HIRDA connection can now authenticate.</p>
        """
    else:
        body = (
            f"<p>Pairing session ended with state "
            f"<strong>{html.escape(state)}</strong>.</p>"
        )

    return f"""<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair HIRDA Device</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:560px;margin:48px auto;padding:0 20px;background:#0b0d10;color:#eef2f6}}
.card{{background:#15191f;border:1px solid #2a313b;border-radius:18px;padding:24px}}
.meta{{color:#9ba7b4;margin-bottom:22px}}
.error{{color:#ff8a80}}
input{{box-sizing:border-box;width:100%;padding:13px;margin:8px 0 16px;border-radius:10px;border:1px solid #46515f;background:#0d1117;color:#fff;font-size:20px;letter-spacing:.08em}}
button{{width:100%;padding:13px;border:0;border-radius:10px;font-weight:700;cursor:pointer}}
label{{display:block;color:#c4ccd5;margin-top:10px}}
</style>
</head>
<body><div class="card">
<h1>Pair HIRDA Device</h1>
<div class="meta">{name} · {platform_name} · {architecture}</div>
{error_html}
{body}
</div></body>
</html>"""
