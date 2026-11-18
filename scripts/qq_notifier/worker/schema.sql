CREATE TABLE IF NOT EXISTS subscriptions (
  group_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, parts TEXT NOT NULL,
  created INTEGER NOT NULL, enqueued INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id),
  group_id TEXT NOT NULL, parts TEXT NOT NULL, next_part INTEGER NOT NULL DEFAULT 0,
  created INTEGER NOT NULL, due INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  sent INTEGER NOT NULL DEFAULT 0, reply_msg_id TEXT
);
CREATE INDEX IF NOT EXISTS notifications_pending ON notifications(sent, due);
CREATE TABLE IF NOT EXISTS runtime_state (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS group_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id TEXT NOT NULL,
  msg_id TEXT NOT NULL,
  msg_idx TEXT,
  sender_id TEXT NOT NULL,
  sender_name TEXT,
  role TEXT,
  is_bot INTEGER NOT NULL DEFAULT 0,
  mentioned INTEGER NOT NULL DEFAULT 0,
  content TEXT NOT NULL,
  quote_text TEXT,
  image_urls TEXT,
  created INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_group_messages_group_created
  ON group_messages(group_id, created DESC);
CREATE INDEX IF NOT EXISTS idx_group_messages_idx
  ON group_messages(group_id, msg_idx);
CREATE INDEX IF NOT EXISTS idx_group_messages_msg_id
  ON group_messages(group_id, msg_id);
CREATE INDEX IF NOT EXISTS idx_events_kind_created
  ON events(kind, created DESC);
CREATE TABLE IF NOT EXISTS group_assistant_state (
  group_id TEXT PRIMARY KEY,
  muted_until INTEGER NOT NULL DEFAULT 0,
  last_welcome_at INTEGER NOT NULL DEFAULT 0,
  last_auto_reply_at INTEGER NOT NULL DEFAULT 0,
  last_reply_at INTEGER NOT NULL DEFAULT 0,
  last_reply_sender TEXT,
  auto_reply_times TEXT
);
CREATE TABLE IF NOT EXISTS redeem_codes (
  code TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  published_at INTEGER NOT NULL,
  created INTEGER NOT NULL,
  announced INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER,
  reward TEXT
);
