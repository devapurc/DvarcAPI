CREATE TABLE IF NOT EXISTS tokens (
  token_hash TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS token_bindings (
  username_key TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  token_hash TEXT NOT NULL REFERENCES tokens(token_hash) ON DELETE CASCADE,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_token_bindings_token ON token_bindings(token_hash);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  starts_at TEXT NOT NULL,
  discord_url TEXT NOT NULL,
  users_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_events_starts_at ON events(starts_at, id);

CREATE TABLE IF NOT EXISTS rate_limits (
  rate_key TEXT NOT NULL,
  bucket INTEGER NOT NULL,
  hits INTEGER NOT NULL,
  PRIMARY KEY(rate_key, bucket)
);
