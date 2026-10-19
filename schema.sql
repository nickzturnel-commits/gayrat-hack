CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at TIMESTAMPTZ,
    note TEXT NOT NULL DEFAULT '',
    drive_file_id TEXT UNIQUE,
    title_override TEXT,
    announce_override TEXT,
    traffic_limit_bytes BIGINT CHECK (traffic_limit_bytes IS NULL OR traffic_limit_bytes BETWEEN 1000000 AND 1000000000000),
    hide_traffic_usage BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'pending_delete')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_synced_at TIMESTAMPTZ,
    last_sync_error TEXT
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS traffic_limit_bytes BIGINT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS hide_traffic_usage BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS servers (
    id TEXT PRIMARY KEY,
    original_name TEXT NOT NULL,
    custom_name TEXT,
    hidden BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    present_upstream BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS user_servers (
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    server_id TEXT NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    hidden BOOLEAN,
    custom_name TEXT,
    PRIMARY KEY (user_id, server_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL
);
