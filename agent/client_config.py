from __future__ import annotations

import json
import os
from pathlib import Path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def mxu_config(root: Path) -> dict | None:
    path = root / "config" / "mxu-MaaNikki.json"
    return read_json(path) if path.is_file() else None


def active_instance(root: Path) -> tuple[str, dict] | None:
    config = mxu_config(root)
    if config is None:
        return None
    instance = next((item for item in config.get("instances", [])
                     if item.get("id") == config.get("lastActiveInstanceId")), None)
    return ("mxu", instance) if instance else None


def active_controller(root: Path) -> dict | None:
    """The submitted controller overrides the tab currently saved by the UI."""
    value = os.environ.get("PI_CONTROLLER")
    if value is not None:
        try:
            controller = json.loads(value)
            return controller if isinstance(controller, dict) else None
        except (ValueError, TypeError):
            return None
    saved = active_instance(root)
    if saved is None:
        return None
    return next((item for item in read_json(root / "interface.json").get("controller", [])
                 if item.get("name") == saved[1].get("controllerName")), None)


def supports_foreground_inputs(controller):
    if not isinstance(controller, dict) or controller.get("type") != "Win32":
        return False
    win32 = controller.get("win32")
    allowed = {"Seize", "SeizeWithBlockInput"}
    return (isinstance(win32, dict) and win32.get("mouse") in allowed
            and win32.get("keyboard") in allowed)


def task_options(task: dict, definitions: dict) -> dict:
    options = []
    for name, value in task.get("optionValues", {}).items():
        definition = definitions.get(name, {})
        item = {"name": name}
        kind = value.get("type")
        if kind == "input":
            item["data"] = value.get("values", {})
        elif kind == "checkbox":
            item["selected_cases"] = value.get("caseNames", [])
        elif kind in {"select", "switch"}:
            chosen = value.get("caseName") if kind == "select" else ("Yes" if value.get("value") else "No")
            item["index"] = next((i for i, case in enumerate(definition.get("cases", []))
                                  if case["name"] == chosen), None)
        options.append(item)
    return {"name": task["taskName"], "option": options}
