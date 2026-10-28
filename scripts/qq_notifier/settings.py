import json
import os
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("CONFIG_FILE", "/etc/nonebot-github/config.json"))
CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))

for key in ("app_id", "app_secret", "repository", "webhook_secret", "binding_code"):
    if not isinstance(CONFIG.get(key), str) or not CONFIG[key].strip():
        raise ValueError(f"Missing configuration: {key}")

if len(CONFIG["webhook_secret"]) < 32 or len(CONFIG["binding_code"]) < 12:
    raise ValueError("Webhook secret or binding code is too short")
