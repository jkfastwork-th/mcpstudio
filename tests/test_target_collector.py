from types import SimpleNamespace

import pytest

from mcp_studio import target_collector


def _monitor():
    return {
        "ok": True,
        "framebuffer": {"width": 1280, "height": 720},
        "selected": {
            "device": "\\\\.\\DISPLAY3",
            "left": 975,
            "top": 2520,
            "width": 1920,
            "height": 1080,
        },
    }


def test_extract_json_payload_ignores_clixml_wrapper():
    text = (
        "Process started with PID 10 (shell: cmd.exe)\n"
        "Initial output:\n"
        "#< CLIXML\r\n"
        '{"window_name":"Instagram","window_class":"Chrome_WidgetWin_1",'
        '"controls":[{"name":"Home","role":"hyperlink","automation_id":"",'
        '"x":987,"y":2840,"width":48,"height":57}]}\r\n'
        '<Objs Version="1.1.0.1">noise</Objs>\n'
    )
    payload = target_collector._extract_json_payload(text)
    assert payload["window_name"] == "Instagram"
    assert payload["controls"][0]["name"] == "Home"


def test_mapped_bounds_transforms_display3_to_vnc_and_clips():
    monitor, frame, _ = target_collector._monitor_geometry(_monitor())
    mapped = target_collector._mapped_bounds(
        {"x": 987, "y": 2840, "width": 48, "height": 57},
        monitor=monitor,
        framebuffer=frame,
    )
    assert mapped == {
        "x": 8.0,
        "y": 213.333,
        "width": 32.0,
        "height": 38.0,
    }

    clipped = target_collector._mapped_bounds(
        {"x": 960, "y": 2510, "width": 30, "height": 30},
        monitor=monitor,
        framebuffer=frame,
    )
    assert clipped == {
        "x": 0.0,
        "y": 0.0,
        "width": 10.0,
        "height": 13.333,
    }

    assert (
        target_collector._mapped_bounds(
            {"x": 0, "y": 0, "width": 100, "height": 100},
            monitor=monitor,
            framebuffer=frame,
        )
        is None
    )


def test_monitor_geometry_for_window_auto_selects_focused_display():
    payload = _monitor()
    payload["monitors"] = [
        payload["selected"],
        {
            "device": "\\\\.\\DISPLAY2",
            "index": 4,
            "left": 2560,
            "top": 348,
            "width": 1920,
            "height": 1080,
        },
    ]
    monitor, frame, device, index, changed = (
        target_collector._monitor_geometry_for_window(
            payload,
            {"x": 2700, "y": 500, "width": 1000, "height": 700},
        )
    )

    assert device == "\\\\.\\DISPLAY2"
    assert index == 4
    assert changed is True
    assert monitor == {
        "left": 2560.0,
        "top": 348.0,
        "width": 1920.0,
        "height": 1080.0,
    }
    assert frame == {"width": 1280.0, "height": 720.0}


class _Registry:
    def __init__(self):
        self.machine = SimpleNamespace(
            machine_id="JKFASTDEV",
            local=False,
            os_name="windows",
        )
        self.calls = []

    def get(self, machine_id):
        assert machine_id == "JKFASTDEV"
        return self.machine

    async def remote_computer_monitors(self, machine):
        assert machine is self.machine
        return _monitor()

    async def remote_call(self, machine, **kwargs):
        self.calls.append((machine, kwargs))
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Process started with PID 42 (shell: cmd.exe)\n"
                        "Initial output:\n"
                        "#< CLIXML\r\n"
                        '{"window_name":"Instagram","window_class":"Chrome_WidgetWin_1",'
                        '"controls":['
                        '{"name":"Home","role":"hyperlink","automation_id":"home",'
                        '"x":987,"y":2840,"width":48,"height":57},'
                        '{"name":"Home","role":"hyperlink","automation_id":"home",'
                        '"x":987,"y":2840,"width":48,"height":57},'
                        '{"name":"Other monitor","role":"button","automation_id":"x",'
                        '"x":10,"y":10,"width":50,"height":50}'
                        "]}\r\n"
                        '<Objs Version="1.1.0.1">noise</Objs>'
                    ),
                }
            ]
        }


@pytest.mark.asyncio
async def test_collect_builds_visual_reflex_request_and_remote_call_shape():
    registry = _Registry()
    collector = target_collector.VisualTargetCollector(registry)
    result = await collector.collect(
        machine_id="JKFASTDEV",
        request_id="jkfastdev:uia:1",
        goal="Select Home",
        workspace_key="mcp-studio",
        session_id="session-1",
    )

    assert result.source == "uia"
    assert result.raw_count == 3
    assert result.target_count == 1
    request = result.request
    assert request["schema"] == "hirda-visual-reflex-request-v1"
    assert request["viewport"] == {"width": 1280.0, "height": 720.0}
    assert request["state"]["window_name"] == "Instagram"
    assert request["targets"][0]["source"] == "uia"
    assert request["targets"][0]["bounds"]["x"] == 8.0
    assert request["targets"][0]["bounds"]["y"] == 213.333

    machine, call = registry.calls[0]
    assert machine is registry.machine
    assert call["tool_name"] == "start_process"
    assert call["workspace_key"] == "mcp-studio"
    assert call["session_id"] == "session-1"
    assert call["arguments"]["shell"] == "cmd.exe"
    assert "-EncodedCommand " in call["arguments"]["command"]


@pytest.mark.asyncio
async def test_collect_rejects_non_windows():
    registry = _Registry()
    registry.machine.os_name = "linux"
    collector = target_collector.VisualTargetCollector(registry)

    with pytest.raises(target_collector.TargetCollectorError, match="requires Windows"):
        await collector.collect(
            machine_id="JKFASTDEV",
            request_id="r1",
            goal="Select Home",
        )
