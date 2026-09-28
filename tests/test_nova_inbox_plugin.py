import hashlib
import hmac
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "nova-inbox"


def load_plugin():
    spec = importlib.util.spec_from_file_location("hirda_nova_inbox_plugin", PLUGIN_ROOT / "adapter.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class InboxHandler(BaseHTTPRequestHandler):
    secret = b""
    received = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        payload = json.loads(raw)
        stamp = self.headers.get("X-Nova-Inbox-Timestamp", "")
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        expected = hmac.new(self.secret, stamp.encode("ascii") + b"\n" + canonical, hashlib.sha256).hexdigest()
        supplied = self.headers.get("X-Nova-Inbox-Signature", "")
        if self.path not in {"/api/inbox/fern/send", "/api/inbox/fern/receipts"} or not hmac.compare_digest(expected, supplied):
            self.send_response(403)
            self.end_headers()
            return
        type(self).received = payload
        if self.path.endswith("/receipts"):
            body = json.dumps({
                "recipient_id": "fern",
                "receipts": [{
                    "message_id": "receipt-mail-test-1-approve",
                    "sender_id": "jack",
                    "recipient_id": "fern",
                    "origin": "approval_receipt",
                    "correlation_id": "turn-test",
                    "payload": {"context": {"receipt": {"action": "approve"}}},
                }],
                "marked_read": ["receipt-mail-test-1-approve"] if payload.get("mark_read") else [],
            }).encode()
            self.send_response(200)
        else:
            body = json.dumps({
                "message_id": "mail-test-1",
                "sender_id": "fern",
                "recipient_id": "jack",
                "type": payload["type"],
                "correlation_id": payload.get("correlation_id"),
            }).encode()
            self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


@pytest.mark.asyncio
async def test_nova_inbox_plugin_sends_verified_fern_mail(monkeypatch, tmp_path):
    secret = b"plugin-test-secret-" * 4
    secret_path = tmp_path / "secret"
    secret_path.write_bytes(secret)
    InboxHandler.secret = secret
    server = ThreadingHTTPServer(("127.0.0.1", 0), InboxHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HIRDA_NOVA_INBOX_URL", f"http://127.0.0.1:{server.server_port}/api/inbox/fern/send")
    monkeypatch.setenv("HIRDA_NOVA_INBOX_SECRET_FILE", str(secret_path))
    try:
        module = load_plugin()
        integration = module.NovaInboxIntegration()
        result = await integration.call_tool(
            "nova_inbox_send",
            {
                "type": "review",
                "subject": "Motion ready",
                "body": "Ready for Jack to review.",
                "priority": "normal",
                "actions": ["discuss"],
            },
            context={
                "workspace": "nova-oracle-desktop",
                "managed_session_id": "ms-test",
                "turn_id": "turn-test",
            },
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result["status"] == "sent"
    assert result["sender_id"] == "fern"
    assert result["sender_verified"] is True
    assert result["governance_authority"] is False
    assert InboxHandler.received["workspace"] == "nova-oracle-desktop"
    assert InboxHandler.received["managed_session_id"] == "ms-test"
    assert InboxHandler.received["correlation_id"] == "turn-test"


@pytest.mark.asyncio
async def test_nova_inbox_plugin_reads_fern_receipts(monkeypatch, tmp_path):
    secret = b"receipt-plugin-secret-" * 4
    secret_path = tmp_path / "secret"
    secret_path.write_bytes(secret)
    InboxHandler.secret = secret
    server = ThreadingHTTPServer(("127.0.0.1", 0), InboxHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "HIRDA_NOVA_INBOX_RECEIPTS_URL",
        f"http://127.0.0.1:{server.server_port}/api/inbox/fern/receipts",
    )
    monkeypatch.setenv("HIRDA_NOVA_INBOX_SECRET_FILE", str(secret_path))
    try:
        module = load_plugin()
        integration = module.NovaInboxIntegration()
        result = await integration.call_tool(
            "nova_inbox_receipts",
            {"unread_only": True, "mark_read": True, "limit": 10},
            context={"workspace": "nova-oracle-desktop"},
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result["status"] == "ok"
    assert result["recipient_id"] == "fern"
    assert result["receipts"][0]["payload"]["context"]["receipt"]["action"] == "approve"
    assert result["marked_read"] == ["receipt-mail-test-1-approve"]
    assert result["governance_authority"] is False


@pytest.mark.asyncio
async def test_nova_inbox_plugin_is_bounded(monkeypatch, tmp_path):
    secret_path = tmp_path / "secret"
    secret_path.write_bytes(b"bounded-secret-" * 4)
    monkeypatch.setenv("HIRDA_NOVA_INBOX_SECRET_FILE", str(secret_path))
    module = load_plugin()
    integration = module.NovaInboxIntegration()
    tools = await integration.list_tools()
    schema = tools[0]["inputSchema"]
    assert [tool["name"] for tool in tools] == ["nova_inbox_send", "nova_inbox_receipts"]
    assert "sender_id" not in schema["properties"]
    assert integration.manifest.permissions["nova_inbox_send"] == "write"
    assert integration.manifest.permissions["nova_inbox_receipts"] == "read"
    status = await integration.status()
    assert status["sender_identity"] == "fern"
    assert status["governance_authority"] is False
