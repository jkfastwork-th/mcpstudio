from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_machine_enrollment_ui_contract() -> None:
    html = (ROOT / "templates" / "index.html").read_text()
    js = (ROOT / "static" / "app.js").read_text()
    css = (ROOT / "static" / "styles.css").read_text()

    for element_id in ("machineDiscoverBtn", "machineEnrollmentSummary", "machineEnrollmentPanel"):
        assert f'id="{element_id}"' in html
        assert element_id in js
    assert "getJson('/api/machines/enrollments')" in js
    assert "sendJson('/api/machines/discover')" in js
    assert "data-machine-approve" in js
    assert "data-machine-reject" in js
    assert "parseWorkspaceMappings" in js
    assert "readonly:{read:true,write:false,execute:false,destructive:false" in js
    assert ".machine-enrollment-row" in css
    assert ".machine-enrollment-actions" in css


def test_enrollment_ui_never_offers_destructive_profile() -> None:
    js = (ROOT / "static" / "app.js").read_text()
    approval = js[js.index("async function approveMachineEnrollment"):js.index("async function rejectMachineEnrollment")]
    assert "destructive:false" in approval
    assert "destructive:true" not in approval
    assert "Read only" in approval
    assert "Read + write" in approval
    assert "Read + write + execute" in approval
