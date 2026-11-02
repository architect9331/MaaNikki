"""MaaNikki MCP server: lets a user's own AI tool help with first-time setup.

Speaks MCP over stdio with the standard library only, so the bundled Python runs it.
Reading is always available. Writing needs --allow-write and the app to be closed,
because the app keeps its config in memory and would overwrite the change on save.
Nothing here starts tasks, controls the game or contacts the network.
"""
from datetime import datetime
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "mxu-MaaNikki.json"
BACKUPS = ROOT / "config" / "ai-backups"
MANUAL = ROOT / "docs" / "manual.md"
PROTOCOL_VERSION = "2025-06-18"
MAX_BACKUPS = 20
START_TASK = "__MXU_PRETASK__start_game"
# Not safe to turn on for users: they exist for development and need manual setup.
HIDDEN_GROUPS = {"developer", "developer_zhaoxi", "developer_xinghai"}


class ToolError(Exception):
    """A problem the AI can explain to the user or correct."""


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_interface():
    data = read_json(ROOT / "interface.json")
    tasks, options, presets, pretasks, global_options = (
        list(data.get("task", [])), dict(data.get("option", {})), list(data.get("preset", [])),
        list(data.get("pretask", [])), list(data.get("global_option", [])))
    for name in data.get("import", []):
        part = read_json(ROOT / name)
        tasks += part.get("task", [])
        options.update(part.get("option", {}))
        presets += part.get("preset", [])
        pretasks += part.get("pretask", [])
        global_options += part.get("global_option", [])
    return {"version": data.get("version"), "tasks": tasks, "options": options, "presets": presets,
            "pretasks": pretasks, "global_options": global_options}


def visible(task):
    return not HIDDEN_GROUPS.intersection(task.get("group", []))


def task_definitions(interface):
    definitions = {task["name"]: task for task in interface["tasks"]}
    for item in interface["pretasks"]:
        definitions[f"__MXU_PRETASK__{item.get('name') or item.get('exec')}"] = {**item, "name": f"__MXU_PRETASK__{item.get('name') or item.get('exec')}"}
    return definitions


def option_keys(option_key, options, found=None):
    """An option and every option nested under its cases."""
    found = [] if found is None else found
    if option_key in found or option_key not in options:
        return found
    found.append(option_key)
    for case in options[option_key].get("cases", []):
        for nested in case.get("option", []):
            option_keys(nested, options, found)
    return found


def describe_option(key, definition):
    kind = definition.get("type", "select")
    result = {"key": key, "type": kind, "label": definition.get("label"),
              "description": definition.get("description")}
    if kind in ("select", "checkbox"):
        result["cases"] = [{"name": case["name"], "label": case.get("label")} for case in definition.get("cases", [])]
        result["default"] = definition.get("default_case")
    elif kind == "switch":
        result["default"] = definition.get("default_case")
    elif kind in ("input", "hotkey"):
        entries = definition.get("inputs" if kind == "input" else "hotkeys", [])
        result["fields"] = [{"name": item["name"], "label": item.get("label"), "default": item.get("default"),
                             "format": item.get("pattern_msg")} for item in entries]
    return result


def running():
    """Whether the app holds the config. Unknown counts as running, so writes stay safe."""
    if os.name != "nt":
        return True
    try:
        output = subprocess.run(["tasklist", "/FI", "IMAGENAME eq MaaNikki.exe", "/NH"], capture_output=True,
                                text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "maanikki.exe" in output.lower()


def load_config():
    if not CONFIG.is_file():
        return None
    return read_json(CONFIG)


def page_of(config, page):
    if config is None:
        raise ToolError("还没有配置文件。请先打开一次 MaaNikki，完成首次设置后关闭，再继续。")
    instances = config.get("instances", [])
    matches = [item for item in instances if item.get("name") == page or item.get("id") == page]
    if len(matches) != 1:
        raise ToolError(f"找不到配置页「{page}」。现有配置页：{[item.get('name') for item in instances]}")
    return matches[0]


def task_of(page, task_name):
    tasks = [task for task in page.get("tasks", []) if task.get("taskName") == task_name]
    if not tasks:
        raise ToolError(f"配置页「{page.get('name')}」没有任务 {task_name}，可以用 add_task 添加。")
    return tasks[0]


def checked_value(key, definition, value):
    """Convert a plain JSON value to the app's stored form, or explain what is wrong."""
    kind = definition.get("type", "select")
    if kind == "switch":
        if not isinstance(value, bool):
            raise ToolError(f"选项 {key} 需要 true 或 false。")
        return {"type": "switch", "value": value}
    names = [case["name"] for case in definition.get("cases", [])]
    if kind == "select":
        if value not in names:
            raise ToolError(f"选项 {key} 只能选择 {names}。")
        return {"type": "select", "caseName": value}
    if kind == "checkbox":
        if not isinstance(value, list) or any(item not in names for item in value):
            raise ToolError(f"选项 {key} 需要从 {names} 中选择的数组。")
        return {"type": "checkbox", "caseNames": value}
    if kind in ("input", "hotkey"):
        entries = definition.get("inputs" if kind == "input" else "hotkeys", [])
        if not isinstance(value, dict) or any(not isinstance(item, str) for item in value.values()):
            raise ToolError(f"选项 {key} 需要字段名到字符串的对象：{[item['name'] for item in entries]}")
        allowed = {item["name"]: item for item in entries}
        for name, text in value.items():
            if name not in allowed:
                raise ToolError(f"选项 {key} 没有字段 {name}。")
            pattern = allowed[name].get("verify")
            if pattern and not re.search(pattern, text):
                raise ToolError(f"{key}.{name} 的值不符合要求：{allowed[name].get('pattern_msg') or pattern}")
        return {"type": kind, "values": {**{item["name"]: item.get("default", "") for item in entries}, **value}}
    raise ToolError(f"选项 {key} 的类型 {kind} 暂不支持修改。")


def save_config(config):
    """Back up, then replace in one step, so a failure leaves the old file intact."""
    BACKUPS.mkdir(parents=True, exist_ok=True)
    backup = BACKUPS / f"mxu-MaaNikki-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:6]}.json"
    backup.write_bytes(CONFIG.read_bytes())
    for old in sorted(BACKUPS.glob("mxu-MaaNikki-*.json"))[:-MAX_BACKUPS]:
        old.unlink(missing_ok=True)
    handle, temp = tempfile.mkstemp(dir=CONFIG.parent, prefix=".mxu-ai-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
        os.replace(temp, CONFIG)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return str(backup.relative_to(ROOT))


# ---------- Reading ----------

def get_setup_status(_):
    interface = load_interface()
    config = load_config()
    status = {
        "version": interface["version"], "app_running": running(), "config_exists": config is not None,
        "can_write": ALLOW_WRITE and not running() and config is not None, "write_enabled": ALLOW_WRITE,
        "first_run_notice_confirmed": False, "pages": [],
        "requirements": ["游戏画面为 16:9，推荐 1920×1080", "核对「设置 → 任务设置 → 游戏键位」与游戏内一致",
                         "所选任务涉及的地图、传送点、能力和关卡已解锁",
                         "使用五星全收集前，清除游戏内祝福闪光仓库的筛选"],
    }
    if config:
        status["first_run_notice_confirmed"] = bool(config.get("settings", {}).get("setupVersion"))
        status["pages"] = [{"name": page.get("name"), "tasks": [
            {"task": task.get("taskName"), "enabled": task.get("enabled", False)} for task in page.get("tasks", [])
        ]} for page in config.get("instances", [])]
    return status


def list_tasks(_):
    interface = load_interface()
    definitions = task_definitions(interface)
    return [{"name": name, "label": task.get("label"), "description": task.get("description"),
             "group": task.get("group", []), "default_enabled": task.get("default_check", False),
             "options": task.get("option", [])}
            for name, task in definitions.items() if visible(task)]


def get_task_options(args):
    interface = load_interface()
    task = task_definitions(interface).get(args["task"])
    if not task or not visible(task):
        raise ToolError(f"没有任务 {args['task']}，可以先调用 list_tasks。")
    keys = []
    for key in task.get("option", []):
        option_keys(key, interface["options"], keys)
    result = {"options": [describe_option(key, interface["options"][key]) for key in keys]}
    page = args.get("page")
    if page:
        saved = task_of(page_of(load_config(), page), args["task"]).get("optionValues", {})
        result["current"] = {key: saved[key] for key in keys if key in saved}
    return result


def read_manual(args):
    text = MANUAL.read_text(encoding="utf-8")
    sections = re.split(r"(?m)^(?=## )", text)
    titles = [section.splitlines()[0][3:] for section in sections if section.startswith("## ")]
    wanted = args.get("section")
    if not wanted:
        return {"sections": titles}
    for section in sections:
        if section.startswith(f"## {wanted}"):
            return {"content": section}
    raise ToolError(f"手册没有章节「{wanted}」。现有章节：{titles}")


# ---------- Writing ----------

def writable_config():
    if not ALLOW_WRITE:
        raise ToolError("当前是只读模式。请让用户在「设置 → AI 助手」开启写入，并用新命令重新添加 MCP。")
    if running():
        raise ToolError("MaaNikki 正在运行，它保存时会覆盖修改。请让用户先关闭软件，然后重试。")
    config = load_config()
    if config is None:
        raise ToolError("还没有配置文件。请先打开一次 MaaNikki，完成首次设置后关闭，再继续。")
    return config


def set_task_enabled(args):
    config = writable_config()
    page = page_of(config, args["page"])
    task = task_of(page, args["task"])
    if not isinstance(args["enabled"], bool):
        raise ToolError("enabled 需要 true 或 false。")
    task["enabled"] = args["enabled"]
    return {"task": args["task"], "enabled": args["enabled"], "backup": save_config(config)}


def add_task(args):
    config = writable_config()
    page = page_of(config, args["page"])
    interface = load_interface()
    definition = task_definitions(interface).get(args["task"])
    if not definition or not visible(definition):
        raise ToolError(f"没有任务 {args['task']}，可以先调用 list_tasks。")
    if any(task.get("taskName") == args["task"] for task in page.get("tasks", [])):
        raise ToolError(f"配置页已经有任务 {args['task']}。")
    page.setdefault("tasks", []).append({"id": uuid4().hex[:7], "taskName": args["task"], "enabled": True,
                                         "optionValues": {}, "expanded": False})
    return {"task": args["task"], "backup": save_config(config)}


def set_task_option(args):
    config = writable_config()
    page = page_of(config, args["page"])
    interface = load_interface()
    definition = task_definitions(interface).get(args["task"])
    if not definition:
        raise ToolError(f"没有任务 {args['task']}。")
    allowed = []
    for key in definition.get("option", []):
        option_keys(key, interface["options"], allowed)
    if args["option"] not in allowed:
        raise ToolError(f"任务 {args['task']} 没有选项 {args['option']}。可用选项：{allowed}")
    task = task_of(page, args["task"])
    value = checked_value(args["option"], interface["options"][args["option"]], args["value"])
    task.setdefault("optionValues", {})[args["option"]] = value
    return {"option": args["option"], "stored": value, "backup": save_config(config)}


def set_game_keys(args):
    config = writable_config()
    interface = load_interface()
    if args["option"] not in interface["global_options"] or not args["option"].startswith("GameKeys"):
        raise ToolError(f"按键选项只能是 {[key for key in interface['global_options'] if key.startswith('GameKeys')]}。")
    value = checked_value(args["option"], interface["options"][args["option"]], args["values"])
    config.setdefault("globalOptionValues", {})[args["option"]] = value
    return {"option": args["option"], "stored": value, "backup": save_config(config)}


PAGE = {"type": "string", "description": "配置页名称，来自 get_setup_status"}
TASK = {"type": "string", "description": "任务名，来自 list_tasks"}
READ_TOOLS = {
    "get_setup_status": (get_setup_status, "首先调用。返回版本、软件是否运行、能否写入、现有配置页和任务，以及开始前要向用户确认的事项。", {}, []),
    "list_tasks": (list_tasks, "列出面向用户的全部任务及其说明和选项。", {}, []),
    "get_task_options": (get_task_options, "返回任务的选项、可选值和默认值；提供 page 时同时返回当前值。", {"task": TASK, "page": PAGE}, ["task"]),
    "read_manual": (read_manual, "读取用户手册。不带 section 时列出章节，带 section 时返回该章节。", {"section": {"type": "string"}}, []),
}
WRITE_TOOLS = {
    "set_task_enabled": (set_task_enabled, "勾选或取消勾选任务。需要先取得用户同意。", {"page": PAGE, "task": TASK, "enabled": {"type": "boolean"}}, ["page", "task", "enabled"]),
    "add_task": (add_task, "把任务添加到配置页末尾。需要先取得用户同意。", {"page": PAGE, "task": TASK}, ["page", "task"]),
    "set_task_option": (set_task_option, "修改任务选项。switch 传布尔值，select 传 case 名，checkbox 传 case 名数组，input 传字段对象。需要先取得用户同意。",
                        {"page": PAGE, "task": TASK, "option": {"type": "string"}, "value": {}}, ["page", "task", "option", "value"]),
    "set_game_keys": (set_game_keys, "修改全局游戏键位，values 是字段名到键名的对象。需要先取得用户同意。", {"option": {"type": "string"}, "values": {"type": "object"}}, ["option", "values"]),
}
ALLOW_WRITE = False


def tools():
    return {**READ_TOOLS, **(WRITE_TOOLS if ALLOW_WRITE else {})}


def schema(name, entry):
    _, description, properties, required = entry
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False}}


def handle(message):
    method, params = message.get("method"), message.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                "serverInfo": {"name": "maanikki", "version": load_interface()["version"]},
                "instructions": "帮助用户完成 MaaNikki 的首次配置。先调用 get_setup_status。修改配置前先说明要改什么并取得同意。这里不能启动任务或操作游戏。"}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [schema(name, entry) for name, entry in tools().items()]}
    if method == "tools/call":
        entry = tools().get(params.get("name"))
        if not entry:
            return text_result(f"没有工具 {params.get('name')}。", True)
        try:
            return text_result(json.dumps(entry[0](params.get("arguments") or {}), ensure_ascii=False, indent=2))
        except ToolError as error:
            return text_result(str(error), True)
        except KeyError as error:
            return text_result(f"缺少参数 {error}。", True)
        except (OSError, ValueError) as error:
            return text_result(f"读取或写入失败：{error}", True)
    raise LookupError(method)


def text_result(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def main():
    global ALLOW_WRITE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-write", action="store_true", help="允许修改配置；默认只读")
    ALLOW_WRITE = parser.parse_args().allow_write
    # Keep Chinese text and line endings intact on a Windows console pipe.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if "id" not in message:
            continue  # Notifications need no reply.
        reply = {"jsonrpc": "2.0", "id": message["id"]}
        try:
            reply["result"] = handle(message)
        except LookupError:
            reply["error"] = {"code": -32601, "message": f"Method not found: {message.get('method')}"}
        print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
