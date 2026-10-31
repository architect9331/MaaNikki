import asyncio
import contextlib
import hashlib
import hmac
import json
import re
import sqlite3
import time
from pathlib import Path

import nonebot
from fastapi import HTTPException, Request
from nonebot import logger, on_message, on_notice
from nonebot.adapters.qq import Bot, Event
from nonebot.adapters.qq.event import (
    GroupAtMessageCreateEvent,
    GroupDelRobotEvent,
    GroupMsgReceiveEvent,
    GroupMsgRejectEvent,
)

from settings import CONFIG

driver = nonebot.get_driver()
app = driver.server_app
repository = CONFIG["repository"]
database = Path(CONFIG.get("database", "/var/lib/nonebot-github/state.sqlite3"))
worker_task = None


def connect():
    connection = sqlite3.connect(database, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextlib.contextmanager
def transaction():
    connection = connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def clean(value, limit=160):
    text = str(value or "").replace("\x00", "")
    if not CONFIG.get("show_links", False):
        text = re.sub(r"https?://\S+|www\.\S+", "[链接]", text)
    return " ".join(text.split())[:limit]


def release_notes(body):
    lines = []
    for line in str(body or "").splitlines():
        line = line.strip()
        if line.startswith(("```", "~~~")):
            continue
        # Preserve the complete notes while converting Markdown to plain text.
        line = re.sub(r"^#{1,6}\s+", "", line)
        line = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "• ", line)
        line = re.sub(r"!?\[([^\]]+)\]\([^)]+\)", r"\1", line)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        lines.append(clean(line, None))
    return "\n".join(lines).strip()


def split_announcement(message):
    # Bound individual QQ messages, never the complete release announcement.
    limit = 1000
    if len(message) <= limit:
        return [message]
    header, _, body = message.partition("\n")
    capacity = limit - len(header) - 32
    chunks = []
    current = ""
    for line in body.splitlines(keepends=True):
        if current and len(current) + len(line) > capacity:
            chunks.append(current.rstrip("\n"))
            current = ""
        while len(line) > capacity:
            chunks.append(line[:capacity])
            line = line[capacity:]
        current += line
    if current.strip():
        chunks.append(current.rstrip("\n"))
    return [f"{header}（{index}/{len(chunks)}）\n{chunk}" for index, chunk in enumerate(chunks, 1)]


def format_event(event_type, payload):
    actor = clean(payload.get("sender", {}).get("login", "GitHub"), 50)
    action = payload.get("action", "")
    header = f"【GitHub · {repository}】"
    url = ""
    if event_type == "push":
        branch = clean(payload.get("ref", "").removeprefix("refs/heads/"))
        commits = payload.get("commits", [])
        verb = "删除分支" if payload.get("deleted") else "推送提交"
        lines = [header, f"{actor} {verb}：{branch}"]
        for commit in commits[:5]:
            title = str(commit.get("message", "")).splitlines()
            lines.append(f"{str(commit.get('id', ''))[:7]} {clean(title[0] if title else '')}")
        if len(commits) > 5:
            lines.append(f"另有 {len(commits) - 5} 条提交")
        url = payload.get("compare", "")
    elif event_type in ("issues", "pull_request"):
        if action not in {"opened", "closed", "reopened", "ready_for_review"}:
            return None
        item = payload.get("issue" if event_type == "issues" else "pull_request", {})
        kind = "PR" if event_type == "pull_request" else "Issue"
        verb = {"opened": "新建", "closed": "关闭", "reopened": "重新打开", "ready_for_review": "请求评审"}[action]
        if kind == "PR" and action == "closed" and item.get("merged"):
            verb = "合并"
        lines = [header, f"{actor} {verb} {kind} #{item.get('number', '')}", clean(item.get("title"))]
        url = item.get("html_url", "")
    elif event_type == "release":
        if action != "published":
            return None
        release = payload.get("release", {})
        if release.get("draft"):
            return None
        lines = [f"【MaaNikki 更新 · {clean(release.get('tag_name'), 60)}】"]
        lines.append(release_notes(release.get("body")) or "新版本已发布。")
        lines.append("更新方式：打开 MaaNikki，检查更新。")
        url = release.get("html_url", "")
        if CONFIG.get("show_links", False) and url:
            lines.append(str(url))
        return "\n".join(lines)
    elif event_type == "workflow_run":
        if action != "completed":
            return None
        run = payload.get("workflow_run", {})
        # Successful workflows are covered by the published release announcement.
        result = {
            "failure": "失败", "timed_out": "超时",
            "action_required": "需要处理", "startup_failure": "启动失败",
        }.get(run.get("conclusion"))
        if result is None:
            return None
        lines = [header, f"工作流 {clean(run.get('name'))}：{result}", f"分支：{clean(run.get('head_branch'))} · #{run.get('run_number', '')}"]
        url = run.get("html_url", "")
    else:
        return None
    if CONFIG.get("show_links", False) and url:
        lines.append(str(url))
    return "\n".join(line for line in lines if line)[:1800]


@app.post("/github/webhook")
async def github_webhook(request: Request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4 * 1024 * 1024:
            raise HTTPException(413, "Payload too large")
    signature = "sha256=" + hmac.new(CONFIG["webhook_secret"].encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.encode(), request.headers.get("x-hub-signature-256", "").encode()):
        raise HTTPException(401, "Invalid signature")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Expected an object")
    event_type = request.headers.get("x-github-event", "")
    if event_type == "ping":
        return {"ok": True}
    if payload.get("repository", {}).get("full_name", "").lower() != repository.lower():
        raise HTTPException(403, "Repository not allowed")
    delivery = request.headers.get("x-github-delivery", "")
    if not delivery or len(delivery) > 100:
        raise HTTPException(400, "Missing or invalid delivery ID")
    message = format_event(event_type, payload)
    parts = split_announcement(message) if message and event_type == "release" else [message]
    with transaction() as connection:
        cursor = connection.execute("INSERT OR IGNORE INTO deliveries(id, created) VALUES (?, ?)", (delivery, time.time()))
        if cursor.rowcount and message:
            groups = connection.execute("SELECT group_id FROM subscriptions WHERE enabled=1").fetchall()
            if event_type == "release" and groups:
                release_id = payload.get("release", {}).get("id")
                if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0:
                    raise HTTPException(400, "Missing or invalid release ID")
                # Redeliveries may have a new delivery ID. Keep release IDs beyond
                # queue cleanup so a published version is announced once.
                announcement = connection.execute(
                    "INSERT OR IGNORE INTO release_announcements(release_id) VALUES (?)",
                    (release_id,),
                )
                if not announcement.rowcount:
                    return {"ok": True}
            connection.executemany(
                "INSERT INTO queue(delivery, group_id, message, parts, due) VALUES (?, ?, ?, ?, ?)",
                [(delivery, group["group_id"], parts[0],
                  json.dumps(parts, ensure_ascii=False) if len(parts) > 1 else None,
                  time.time()) for group in groups],
            )
    return {"ok": True}


async def deliver_messages():
    last_cleanup = 0
    while True:
        try:
            now = time.time()
            if now - last_cleanup > 3600:
                with transaction() as connection:
                    connection.execute("DELETE FROM queue WHERE created < ?", (now - 30 * 86400,))
                    connection.execute("DELETE FROM deliveries WHERE created < ? AND id NOT IN (SELECT delivery FROM queue)", (now - 7 * 86400,))
                last_cleanup = now
            bots = [bot for bot in nonebot.get_bots().values() if isinstance(bot, Bot)]
            if not bots:
                await asyncio.sleep(5)
                continue
            with transaction() as connection:
                row = connection.execute(
                    "SELECT q.* FROM queue q JOIN subscriptions s ON s.group_id=q.group_id "
                    "WHERE q.sent=0 AND q.attempts<12 AND q.due<=? AND s.enabled=1 ORDER BY q.id LIMIT 1",
                    (now,),
                ).fetchone()
            if row is None:
                await asyncio.sleep(2)
                continue
            parts = json.loads(row["parts"]) if row["parts"] else [row["message"]]
            try:
                await bots[0].send_to_group(group_openid=row["group_id"], message=parts[row["next_part"]])
            except Exception as error:
                code = getattr(error, "code", None)
                # Log only the error type/code, never message bodies or credentials.
                logger.warning(f"QQ delivery failed: {type(error).__name__}, code={code}")
                with transaction() as connection:
                    if code in {40034101, 40034105, 40054003, 40054002}:
                        connection.execute("UPDATE subscriptions SET enabled=0 WHERE group_id=?", (row["group_id"],))
                    connection.execute("UPDATE queue SET attempts=attempts+1, due=? WHERE id=?", (now + min(3600, 30 * 2 ** row["attempts"]), row["id"]))
            else:
                with transaction() as connection:
                    next_part = row["next_part"] + 1
                    connection.execute(
                        "UPDATE queue SET next_part=?, sent=?, attempts=0, due=? WHERE id=?",
                        (next_part, int(next_part == len(parts)), time.time(), row["id"]),
                    )
            # At most 15 notifications/minute, below official group and bot limits.
            await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(f"Notification worker error: {type(error).__name__}")
            await asyncio.sleep(10)


@driver.on_startup
async def startup():
    global worker_task
    database.parent.mkdir(parents=True, exist_ok=True)
    with transaction() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                group_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS release_announcements (
                release_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY, delivery TEXT NOT NULL REFERENCES deliveries(id),
                group_id TEXT NOT NULL, message TEXT NOT NULL,
                created REAL NOT NULL DEFAULT (unixepoch()), due REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, sent INTEGER NOT NULL DEFAULT 0,
                UNIQUE(delivery, group_id)
            );
            CREATE INDEX IF NOT EXISTS queue_pending ON queue(sent, due);
        """)
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(queue)")}
        if "parts" not in columns:
            connection.execute("ALTER TABLE queue ADD COLUMN parts TEXT")
        if "next_part" not in columns:
            connection.execute("ALTER TABLE queue ADD COLUMN next_part INTEGER NOT NULL DEFAULT 0")
    worker_task = asyncio.create_task(deliver_messages())
    logger.warning("GitHub notifier started; waiting for QQ connection and group binding")


@driver.on_shutdown
async def shutdown():
    if worker_task:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task


@driver.on_bot_connect
async def connected(bot: Bot):
    logger.warning("QQ official bot connected")


@driver.on_bot_disconnect
async def disconnected(bot: Bot):
    logger.warning("QQ official bot disconnected; pending notifications retained")


messages = on_message(priority=10, block=False)


@messages.handle()
async def handle_message(bot: Bot, event: Event):
    if not isinstance(event, GroupAtMessageCreateEvent):
        return
    text = event.get_plaintext().strip().removeprefix("/")
    command, _, argument = text.partition(" ")
    if command not in {"绑定GitHub", "取消GitHub", "GitHub状态", "GitHub帮助"}:
        return
    group_id = event.group_openid
    if command in {"绑定GitHub", "取消GitHub"}:
        if not hmac.compare_digest(argument.strip().encode(), CONFIG["binding_code"].encode()):
            await messages.finish("绑定码不正确，请联系机器人管理员。")
        with transaction() as connection:
            if command == "绑定GitHub":
                other = connection.execute("SELECT 1 FROM subscriptions WHERE group_id<>?", (group_id,)).fetchone()
                if other:
                    response = "机器人已绑定其他群，请联系机器人管理员更换。"
                else:
                    connection.execute("INSERT INTO subscriptions(group_id, enabled) VALUES (?, 1) ON CONFLICT(group_id) DO UPDATE SET enabled=1", (group_id,))
                    response = f"已订阅 {repository} 的提交、Issue、PR、版本发布和工作流异常。请在机器人资料中开启主动消息。"
            else:
                connection.execute("DELETE FROM queue WHERE group_id=?", (group_id,))
                connection.execute("DELETE FROM subscriptions WHERE group_id=?", (group_id,))
                response = "本群已取消 GitHub 通知。"
        await messages.finish(response)
    elif command == "GitHub状态":
        with transaction() as connection:
            subscription = connection.execute("SELECT enabled FROM subscriptions WHERE group_id=?", (group_id,)).fetchone()
            pending = connection.execute("SELECT count(*) FROM queue WHERE group_id=? AND sent=0 AND attempts<12", (group_id,)).fetchone()[0]
            failed = connection.execute("SELECT count(*) FROM queue WHERE group_id=? AND sent=0 AND attempts>=12", (group_id,)).fetchone()[0]
        state = "未绑定" if subscription is None else ("接收中" if subscription["enabled"] else "已暂停，请开启主动消息后重新绑定")
        await messages.finish(f"{repository}\n通知状态：{state}\n待发送：{pending} 条；发送失败：{failed} 条")
    else:
        await messages.finish("GitHub 通知指令：\n绑定GitHub 绑定码\nGitHub状态\n取消GitHub 绑定码\n请先 @机器人，再发送指令。")


notices = on_notice(priority=10, block=False)


@notices.handle()
async def handle_notice(event: Event):
    if not isinstance(event, (GroupDelRobotEvent, GroupMsgRejectEvent, GroupMsgReceiveEvent)):
        return
    with transaction() as connection:
        if isinstance(event, GroupDelRobotEvent):
            connection.execute("DELETE FROM queue WHERE group_id=?", (event.group_openid,))
            connection.execute("DELETE FROM subscriptions WHERE group_id=?", (event.group_openid,))
        else:
            connection.execute("UPDATE subscriptions SET enabled=? WHERE group_id=?", (int(isinstance(event, GroupMsgReceiveEvent)), event.group_openid))
