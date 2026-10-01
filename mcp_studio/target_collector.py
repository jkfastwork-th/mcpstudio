from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass
from typing import Any

from .visual_reflex import VISUAL_REFLEX_REQUEST_SCHEMA


TARGET_COLLECTION_SCHEMA = "hirda-visual-target-collection-v1"
DEFAULT_MAX_TARGETS = 32

_UIA_POWERSHELL = r"""
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$OutputEncoding=[System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient

$f=[System.Windows.Automation.AutomationElement]::FocusedElement
if($null -eq $f){
  [pscustomobject]@{window_name='';window_class='';window_bounds=$null;controls=@()} | ConvertTo-Json -Compress -Depth 5
  exit
}

$walker=[System.Windows.Automation.TreeWalker]::ControlViewWalker
$scope=$f
$cursor=$f
$document=$null
while($true){
  if($cursor.Current.ControlType.ProgrammaticName -eq 'ControlType.Document'){
    $document=$cursor
    break
  }
  $p=$walker.GetParent($cursor)
  if($null -eq $p){break}
  if($p -eq [System.Windows.Automation.AutomationElement]::RootElement){break}
  $cursor=$p
  $scope=$cursor
}
if($null -ne $document){
  $scope=$document
}

$wanted=@(
  'ControlType.Button',
  'ControlType.Edit',
  'ControlType.MenuItem',
  'ControlType.TabItem',
  'ControlType.Hyperlink',
  'ControlType.CheckBox',
  'ControlType.RadioButton',
  'ControlType.ComboBox',
  'ControlType.ListItem'
)

$all=$scope.FindAll(
  [System.Windows.Automation.TreeScope]::Descendants,
  [System.Windows.Automation.Condition]::TrueCondition
)
$out=@()
for($i=0;$i -lt $all.Count;$i++){
  try{
    $c=$all.Item($i).Current
    $ct=$c.ControlType.ProgrammaticName
    if($wanted -notcontains $ct){continue}
    if(-not $c.IsEnabled){continue}
    $r=$c.BoundingRectangle
    if($r.Width -le 1 -or $r.Height -le 1){continue}
    $name=$c.Name
    if([string]::IsNullOrWhiteSpace($name)){$name=$c.AutomationId}
    if([string]::IsNullOrWhiteSpace($name)){continue}
    $out += [pscustomobject]@{
      name=$name
      role=$ct.Replace('ControlType.','').ToLower()
      automation_id=$c.AutomationId
      x=[math]::Round($r.X,2)
      y=[math]::Round($r.Y,2)
      width=[math]::Round($r.Width,2)
      height=[math]::Round($r.Height,2)
    }
    if($out.Count -ge 160){break}
  }catch{}
}

$windowRect=$scope.Current.BoundingRectangle
[pscustomobject]@{
  window_name=$scope.Current.Name
  window_class=$scope.Current.ClassName
  window_bounds=[pscustomobject]@{
    x=[math]::Round($windowRect.X,2)
    y=[math]::Round($windowRect.Y,2)
    width=[math]::Round($windowRect.Width,2)
    height=[math]::Round($windowRect.Height,2)
  }
  controls=@($out)
} | ConvertTo-Json -Compress -Depth 5
"""


class TargetCollectorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TargetCollection:
    request: dict[str, Any]
    source: str
    window_name: str
    window_class: str
    selected_monitor: str
    raw_count: int
    target_count: int
    collect_ms: float

    def summary(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("request", None)
        data["schema"] = TARGET_COLLECTION_SCHEMA
        return data


def _finite_number(value: Any, *, field: str, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise TargetCollectorError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TargetCollectorError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise TargetCollectorError(f"{field} must be finite")
    if positive and number <= 0:
        raise TargetCollectorError(f"{field} must be greater than zero")
    return number


def _remote_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        raise TargetCollectorError("remote collector returned invalid content")
    chunks = [
        str(item.get("text") or "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    text = "\n".join(chunk for chunk in chunks if chunk)
    if not text:
        raise TargetCollectorError("remote collector returned no text")
    return text


def _extract_json_payload(text: str) -> dict[str, Any]:
    for line in str(text or "").splitlines():
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("controls"), list):
            return payload
    raise TargetCollectorError("UI Automation JSON payload was not found")


def _monitor_geometry(payload: dict[str, Any]) -> tuple[dict[str, float], dict[str, float], str]:
    selected = payload.get("selected")
    framebuffer = payload.get("framebuffer")
    if not isinstance(selected, dict) or not isinstance(framebuffer, dict):
        raise TargetCollectorError("remote monitor status is incomplete")

    monitor = {
        "left": _finite_number(selected.get("left"), field="selected.left"),
        "top": _finite_number(selected.get("top"), field="selected.top"),
        "width": _finite_number(selected.get("width"), field="selected.width", positive=True),
        "height": _finite_number(selected.get("height"), field="selected.height", positive=True),
    }
    frame = {
        "width": _finite_number(
            framebuffer.get("width"), field="framebuffer.width", positive=True
        ),
        "height": _finite_number(
            framebuffer.get("height"), field="framebuffer.height", positive=True
        ),
    }
    device = str(selected.get("device") or "").strip()
    return monitor, frame, device


def _monitor_geometry_for_window(
    monitor_payload: dict[str, Any],
    window_bounds: Any,
) -> tuple[dict[str, float], dict[str, float], str, int | None, bool]:
    selected_monitor, framebuffer, selected_device = _monitor_geometry(monitor_payload)
    selected = monitor_payload.get("selected")
    selected_index: int | None = None
    if isinstance(selected, dict):
        selected_index_value = selected.get("index")
        if type(selected_index_value) is int:
            selected_index = selected_index_value

    if not isinstance(window_bounds, dict):
        return selected_monitor, framebuffer, selected_device, selected_index, False

    try:
        wx = _finite_number(window_bounds.get("x"), field="window_bounds.x")
        wy = _finite_number(window_bounds.get("y"), field="window_bounds.y")
        ww = _finite_number(
            window_bounds.get("width"),
            field="window_bounds.width",
            positive=True,
        )
        wh = _finite_number(
            window_bounds.get("height"),
            field="window_bounds.height",
            positive=True,
        )
    except TargetCollectorError:
        return selected_monitor, framebuffer, selected_device, selected_index, False

    raw_monitors = monitor_payload.get("monitors")
    if not isinstance(raw_monitors, list):
        return selected_monitor, framebuffer, selected_device, selected_index, False

    best: tuple[float, dict[str, float], str, int | None] | None = None
    for raw in raw_monitors:
        if not isinstance(raw, dict):
            continue
        try:
            left = _finite_number(raw.get("left"), field="monitor.left")
            top = _finite_number(raw.get("top"), field="monitor.top")
            width = _finite_number(raw.get("width"), field="monitor.width", positive=True)
            height = _finite_number(
                raw.get("height"),
                field="monitor.height",
                positive=True,
            )
        except TargetCollectorError:
            continue

        intersection_width = max(
            0.0,
            min(wx + ww, left + width) - max(wx, left),
        )
        intersection_height = max(
            0.0,
            min(wy + wh, top + height) - max(wy, top),
        )
        area = intersection_width * intersection_height
        if area <= 0:
            continue

        index = raw.get("index")
        normalized_index = (
            int(index)
            if isinstance(index, int) and not isinstance(index, bool)
            else None
        )
        candidate = (
            area,
            {
                "left": left,
                "top": top,
                "width": width,
                "height": height,
            },
            str(raw.get("device") or "").strip(),
            normalized_index,
        )
        if best is None or candidate[0] > best[0]:
            best = candidate

    if best is None:
        return selected_monitor, framebuffer, selected_device, selected_index, False

    _area, monitor, device, index = best
    changed = index is not None and index != selected_index
    return monitor, framebuffer, device, index, changed


def _mapped_bounds(
    raw: dict[str, Any],
    *,
    monitor: dict[str, float],
    framebuffer: dict[str, float],
) -> dict[str, float] | None:
    x = _finite_number(raw.get("x"), field="control.x")
    y = _finite_number(raw.get("y"), field="control.y")
    width = _finite_number(raw.get("width"), field="control.width", positive=True)
    height = _finite_number(raw.get("height"), field="control.height", positive=True)

    left = max(x, monitor["left"])
    top = max(y, monitor["top"])
    right = min(x + width, monitor["left"] + monitor["width"])
    bottom = min(y + height, monitor["top"] + monitor["height"])
    if right <= left or bottom <= top:
        return None

    scale_x = framebuffer["width"] / monitor["width"]
    scale_y = framebuffer["height"] / monitor["height"]
    return {
        "x": round((left - monitor["left"]) * scale_x, 3),
        "y": round((top - monitor["top"]) * scale_y, 3),
        "width": round((right - left) * scale_x, 3),
        "height": round((bottom - top) * scale_y, 3),
    }


def _target_id(raw: dict[str, Any], mapped: dict[str, float]) -> str:
    seed = "|".join(
        [
            str(raw.get("role") or ""),
            str(raw.get("name") or ""),
            str(raw.get("automation_id") or ""),
            *(f"{mapped[key]:.3f}" for key in ("x", "y", "width", "height")),
        ]
    )
    return "uia:" + hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:12]


def _encoded_powershell() -> str:
    return base64.b64encode(_UIA_POWERSHELL.encode("utf-16le")).decode("ascii")


class VisualTargetCollector:
    def __init__(self, registry: Any) -> None:
        self.registry = registry

    async def collect(
        self,
        *,
        machine_id: str,
        request_id: str,
        goal: str,
        workspace_key: str = "mcp-studio",
        project_path: str | None = None,
        session_id: str = "visual-reflex-target-collector",
        max_targets: int = DEFAULT_MAX_TARGETS,
    ) -> TargetCollection:
        started = time.perf_counter()
        normalized_machine = str(machine_id or "").strip()
        normalized_request = str(request_id or "").strip()
        normalized_goal = str(goal or "").strip()
        if not normalized_machine:
            raise TargetCollectorError("machine_id is required")
        if not normalized_request:
            raise TargetCollectorError("request_id is required")
        if not normalized_goal:
            raise TargetCollectorError("goal is required")
        if not 1 <= int(max_targets) <= 64:
            raise TargetCollectorError("max_targets must be between 1 and 64")

        try:
            machine = self.registry.get(normalized_machine)
        except Exception as exc:
            raise TargetCollectorError(f"unknown machine: {normalized_machine}") from exc
        if bool(getattr(machine, "local", False)):
            raise TargetCollectorError("UIA target collection requires a remote Windows machine")
        os_name = str(getattr(machine, "os_name", "") or "").strip().lower()
        if not os_name.startswith("win"):
            raise TargetCollectorError("UIA target collection requires Windows")

        try:
            monitors = await self.registry.remote_computer_monitors(machine)
            result = await self.registry.remote_call(
                machine,
                tool_name="start_process",
                arguments={
                    "command": (
                        "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
                        + _encoded_powershell()
                    ),
                    "timeout_ms": 8000,
                    "shell": "cmd.exe",
                },
                workspace_key=workspace_key,
                project_path=project_path,
                session_id=session_id,
            )
        except Exception as exc:
            raise TargetCollectorError(
                f"remote UIA collection failed: {type(exc).__name__}: {exc}"
            ) from exc

        payload = _extract_json_payload(_remote_text(result))
        (
            monitor,
            framebuffer,
            device,
            monitor_index,
            monitor_changed,
        ) = _monitor_geometry_for_window(monitors, payload.get("window_bounds"))
        if monitor_changed and monitor_index is not None:
            try:
                await self.registry.set_remote_computer_monitor(
                    machine,
                    mode=f"monitor:{monitor_index}",
                )
            except Exception as exc:
                raise TargetCollectorError(
                    f"remote monitor auto-selection failed: {type(exc).__name__}: {exc}"
                ) from exc

        raw_controls = payload.get("controls")
        if not isinstance(raw_controls, list):
            raise TargetCollectorError("UI Automation controls must be an array")

        targets: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, float, float, float, float]] = set()
        seen_ids: set[str] = set()
        for raw in raw_controls:
            if not isinstance(raw, dict):
                continue
            label = str(raw.get("name") or "").strip()
            role = str(raw.get("role") or "control").strip().lower() or "control"
            automation_id = str(raw.get("automation_id") or "").strip()
            if not label:
                continue
            try:
                mapped = _mapped_bounds(
                    raw,
                    monitor=monitor,
                    framebuffer=framebuffer,
                )
            except TargetCollectorError:
                continue
            if mapped is None:
                continue

            key = (
                role,
                label,
                automation_id,
                mapped["x"],
                mapped["y"],
                mapped["width"],
                mapped["height"],
            )
            if key in seen:
                continue
            seen.add(key)

            target_id = _target_id(raw, mapped)
            if target_id in seen_ids:
                continue
            seen_ids.add(target_id)
            targets.append(
                {
                    "target_id": target_id,
                    "label": label[:500],
                    "role": role[:120],
                    "source": "uia",
                    "bounds": mapped,
                    "confidence": 0.98,
                }
            )
            if len(targets) >= int(max_targets):
                break

        if not targets:
            raise TargetCollectorError("no actionable UIA targets are visible on the selected monitor")

        request = {
            "schema": VISUAL_REFLEX_REQUEST_SCHEMA,
            "request_id": normalized_request,
            "goal": normalized_goal,
            "machine_id": normalized_machine,
            "viewport": {
                "width": framebuffer["width"],
                "height": framebuffer["height"],
            },
            "state": {
                "collector": "uia",
                "provider": "windows_ui_automation",
                "window_name": str(payload.get("window_name") or "").strip()[:500],
                "window_class": str(payload.get("window_class") or "").strip()[:200],
                "selected_monitor": device,
            },
            "targets": targets,
        }
        return TargetCollection(
            request=request,
            source="uia",
            window_name=str(payload.get("window_name") or "").strip(),
            window_class=str(payload.get("window_class") or "").strip(),
            selected_monitor=device,
            raw_count=len(raw_controls),
            target_count=len(targets),
            collect_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )
