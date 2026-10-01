from mcp_studio.device_pairing_web import pairing_policy_from_form, render_pairing_page


def test_pairing_page_requests_8_digit_code_and_carries_nonce():
    html = render_pairing_page(
        {
            "session_id": "pair_abc",
            "name": "JKFASTDEV",
            "platform": "windows",
            "architecture": "x86_64",
            "state": "pairing",
            "capabilities": ["filesystem", "process"],
        },
        nonce="nonce-secret",
    )
    assert 'action="/pair/pair_abc/verify"' in html
    assert 'name="nonce" value="nonce-secret"' in html
    assert 'autocomplete="one-time-code"' in html
    assert "JKFASTDEV" in html


def test_pending_owner_page_does_not_expose_owner_token_field_when_session_valid():
    html = render_pairing_page(
        {
            "session_id": "pair_abc",
            "name": "openclaw",
            "platform": "linux",
            "architecture": "x86_64",
            "state": "pending_owner",
            "capabilities": ["filesystem", "process"],
        },
        owner_session_valid=True,
    )
    assert 'action="/pair/pair_abc/approve"' in html
    assert 'name="owner_token"' not in html
    assert 'name="policy_read" value="1" checked' in html
    assert 'name="policy_execute" value="1" checked' in html
    assert 'name="policy_write" value="1" checked' not in html
    assert 'name="policy_destructive" value="1" checked' not in html


def test_pairing_policy_from_form_returns_explicit_booleans():
    policy = pairing_policy_from_form(
        {
            "policy_read": ["1"],
            "policy_execute": ["1"],
            "policy_write": ["0"],
        }
    )
    assert policy == {
        "read": True,
        "write": False,
        "execute": True,
        "destructive": False,
    }


def test_pairing_page_escapes_device_metadata():
    html = render_pairing_page(
        {
            "session_id": "pair_abc",
            "name": "<script>alert(1)</script>",
            "platform": "linux",
            "architecture": "x86_64",
            "state": "trusted",
            "capabilities": [],
        }
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
