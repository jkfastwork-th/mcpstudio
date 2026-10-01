from __future__ import annotations

import html
from typing import Any


def pairing_policy_from_form(parsed: dict[str, list[str]]) -> dict[str, bool]:
    return {
        "read": (parsed.get("policy_read") or [""])[-1] == "1",
        "write": (parsed.get("policy_write") or [""])[-1] == "1",
        "execute": (parsed.get("policy_execute") or [""])[-1] == "1",
        "destructive": (parsed.get("policy_destructive") or [""])[-1] == "1",
    }


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
    requested_capabilities = {str(item) for item in (session.get("capabilities") or [])}
    capabilities = ", ".join(
        html.escape(item) for item in requested_capabilities
    ) or "none"
    read_checked = " checked"
    execute_checked = " checked" if "process" in requested_capabilities else ""
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
          <div class="permissions">
            <p class="section-title">Device permissions</p>
            <label class="permission">
              <input type="checkbox" name="policy_read" value="1"{read_checked}>
              <span><strong>Read</strong><small>Inspect files and workspace state.</small></span>
            </label>
            <label class="permission">
              <input type="checkbox" name="policy_write" value="1">
              <span><strong>Write</strong><small>Create or modify workspace files.</small></span>
            </label>
            <label class="permission">
              <input type="checkbox" name="policy_execute" value="1"{execute_checked}>
              <span><strong>Execute</strong><small>Run non-write processes. Preselected when process capability is requested.</small></span>
            </label>
            <label class="permission danger">
              <input type="checkbox" name="policy_destructive" value="1">
              <span><strong>Destructive</strong><small>Allow destructive operations. Leave off unless explicitly needed.</small></span>
            </label>
          </div>
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
input[type="password"],input[name="code"]{{box-sizing:border-box;width:100%;padding:13px;margin:8px 0 16px;border-radius:10px;border:1px solid #46515f;background:#0d1117;color:#fff;font-size:20px;letter-spacing:.08em}}
.permissions{{margin:18px 0;padding:14px;border:1px solid #2a313b;border-radius:12px;background:#0d1117}}
.section-title{{margin:0 0 8px;font-weight:700}}
.permission{{display:flex;gap:12px;align-items:flex-start;padding:10px 0;margin:0;color:#c4ccd5;cursor:pointer}}
.permission input{{width:auto;margin:3px 0 0;accent-color:#eef2f6}}
.permission span{{display:flex;flex-direction:column;gap:3px}}
.permission small{{color:#8793a1;line-height:1.35}}
.permission.danger small{{color:#d6a5a1}}
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
