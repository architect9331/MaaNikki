"""Convert a legacy notifier SQLite backup into cf d1 query batches."""

import argparse
import json
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if not args.source.is_file():
        parser.error("Source SQLite backup does not exist")
    connection = sqlite3.connect(args.source.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    statements = []

    def add(sql, *params):
        statements.append({"sql": sql, "params": list(params)})

    subscriptions = connection.execute("SELECT * FROM subscriptions").fetchall()
    for row in subscriptions:
        add("INSERT INTO subscriptions(group_id,enabled) VALUES(?,?) "
            "ON CONFLICT(group_id) DO UPDATE SET enabled=excluded.enabled",
            row["group_id"], row["enabled"])
    deliveries = connection.execute("SELECT * FROM deliveries").fetchall()
    for row in deliveries:
        add("INSERT OR IGNORE INTO events(id,kind,parts,created,enqueued) VALUES(?,'legacy','[]',?,1)",
            "delivery:" + row["id"], int(row["created"]))
    releases = connection.execute("SELECT * FROM release_announcements").fetchall()
    for row in releases:
        add("INSERT OR IGNORE INTO events(id,kind,parts,created,enqueued) VALUES(?,'legacy_release','[]',0,1)",
            "release:" + str(row["release_id"]))
    queue = connection.execute("SELECT * FROM queue ORDER BY id").fetchall()
    for row in queue:
        parts = json.loads(row["parts"]) if row["parts"] else [row["message"]]
        if not isinstance(parts, list) or not parts or not all(isinstance(part, str) for part in parts):
            raise ValueError("Invalid legacy message parts")
        key = "delivery:" + row["delivery"]
        add("INSERT INTO notifications(id,event_id,group_id,parts,next_part,created,due,attempts,sent) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "next_part=MAX(notifications.next_part,excluded.next_part),"
            "sent=MAX(notifications.sent,excluded.sent)",
            key + ":" + row["group_id"], key, row["group_id"],
            json.dumps(parts, ensure_ascii=False), row["next_part"],
            int(row["created"]), int(row["due"]), row["attempts"], row["sent"])
    connection.close()
    args.output.mkdir(parents=True, exist_ok=False)
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    schema_batch = [{"sql": sql.strip()} for sql in schema.split(";") if sql.strip()]
    (args.output / "000-schema.json").write_text(json.dumps(schema_batch), encoding="utf-8")
    for start in range(0, len(statements), 50):
        (args.output / f"{start // 50 + 1:03d}-state.json").write_text(
            json.dumps(statements[start:start + 50], ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"subscriptions": len(subscriptions), "deliveries": len(deliveries),
                      "releases": len(releases), "messages": len(queue),
                      "pending": sum(not row["sent"] and row["attempts"] < 12 for row in queue)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
