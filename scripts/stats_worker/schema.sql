CREATE TABLE IF NOT EXISTS reporters (
  id TEXT PRIMARY KEY, day TEXT NOT NULL, excluded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, day TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rolls (
  reporter TEXT NOT NULL, catalog TEXT NOT NULL, name TEXT NOT NULL, slot TEXT NOT NULL, color TEXT NOT NULL,
  count INTEGER NOT NULL, PRIMARY KEY (reporter, catalog, name, slot, color)
);
CREATE TABLE IF NOT EXISTS bling_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, reporter TEXT NOT NULL, day TEXT NOT NULL, app TEXT NOT NULL,
  energy INTEGER NOT NULL, gain INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dig_targets (
  reporter TEXT NOT NULL, item TEXT NOT NULL, count INTEGER NOT NULL,
  PRIMARY KEY (reporter, item)
);
CREATE TABLE IF NOT EXISTS reporter_preferences (
  reporter TEXT NOT NULL, day TEXT NOT NULL, app TEXT NOT NULL,
  category TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY (reporter, category, value)
);
