from __future__ import annotations

import copy
import json
from pathlib import Path
from client_config import active_instance, task_options, read_json


ROOT = Path(__file__).resolve().parents[2]
TASKS = ROOT / "tasks"


def foreground_inputs() -> bool:
    saved = active_instance(ROOT)
    if saved is None:
        return False
    controller = next((item for item in read_json(ROOT / "interface.json").get("controller", [])
                       if item["name"] == saved[1].get("controllerName")), {})
    win32 = controller.get("win32", {})
    allowed = {"Seize", "SeizeWithBlockInput"}
    return win32.get("mouse") in allowed and win32.get("keyboard") in allowed


def saved_task(name: str) -> dict | None:
    saved = active_instance(ROOT)
    if saved is None:
        return None
    task = next((item for item in saved[1].get("tasks", []) if item.get("taskName") == name), None)
    if task is None:
        return None
    definition = next((read_json(path) for path in TASKS.glob("*.json")
                       if any(item.get("name") == name for item in read_json(path).get("task", []))), {})
    return task_options(task, definition.get("option", {}))


def option_overrides(task: dict, definition: dict, extra_roots=()) -> dict:
    result = {}
    definitions = definition.get("option", {})
    stored = {}

    def remember(options):
        for item in options:
            stored[item["name"]] = item
            remember(item.get("sub_options", []))
    remember(task.get("option", []))

    def merge(value):
        for node, fields in value.items():
            result.setdefault(node, {}).update(copy.deepcopy(fields))

    def visit(name):
        option = definitions.get(name, {})
        saved = stored.get(name, {})
        inputs = option.get("inputs", [])
        values = {v["name"]: saved.get("data", {}).get(v["name"], v.get("default", "")) for v in inputs}
        raw = json.dumps(option.get("pipeline_override", {}), ensure_ascii=False)
        for item in inputs:
            token, value = "{" + item["name"] + "}", str(values[item["name"]])
            if item.get("pipeline_type") == "int":
                if not value.isdigit():
                    raise ValueError("保存的数字设置无效")
                raw = raw.replace(json.dumps(token), str(int(value)))
            else:
                raw = raw.replace(token, json.dumps(value, ensure_ascii=False)[1:-1])
        merge(json.loads(raw))
        cases = option.get("cases", [])
        if not cases:
            return
        if option.get("type") == "checkbox":
            selection = saved.get("selected_cases", option.get("default_case", []))
            chosen = [c for c in cases if c["name"] in selection]
        else:
            index = saved.get("index")
            if not isinstance(index, int) or not 0 <= index < len(cases):
                index = next((i for i, c in enumerate(cases) if c["name"] == option.get("default_case")), 0)
            chosen = [cases[index]]
        for case in chosen:
            merge(case.get("pipeline_override", {}))
            for child in case.get("option", []):
                visit(child)
    for name in [*definition.get("task", [{}])[0].get("option", []), *extra_roots]:
        visit(name)
    return result


def realm_settings() -> tuple[str, dict]:
    task = saved_task("realm_challenge")
    if task is None:
        raise ValueError("无法确定当前配置的幻境设置，请先保存任务设置。")
    definition = json.loads((TASKS / "Energy.json").read_text(encoding="utf-8"))
    overrides = option_overrides(task, definition, ("JihuaTarget", "JihuaMaterialMode", "JihuaMaterials", "BlessTarget", "MonsterTarget"))
    next_nodes = overrides.get("MaaNikki_RealmEnergy", {}).get("next", ["MaaNikki_Bless"])
    names = {"MaaNikki_Jihua": "jihua", "MaaNikki_Bless": "bless", "MaaNikki_Monster": "monster"}
    return names.get(next_nodes[0], "bless"), overrides
