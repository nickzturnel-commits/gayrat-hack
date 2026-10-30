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
    include_sas BOOLEAN NOT NULL DEFAULT TRUE,
    device_limit INTEGER CHECK (device_limit IS NULL OR device_limit BETWEEN 1 AND 99),
    device_limit_status TEXT NOT NULL DEFAULT 'none' CHECK (device_limit_status IN ('none', 'waiting', 'ready', 'error')),
    device_limit_error TEXT,
    happ_install_code TEXT,
    limited_link TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'pending_delete')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_synced_at TIMESTAMPTZ,
    last_sync_error TEXT
);

ALTER TABLE users ADD COLUMN IF NOT EXISTS traffic_limit_bytes BIGINT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS hide_traffic_usage BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS include_sas BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS device_limit INTEGER CHECK (device_limit IS NULL OR device_limit BETWEEN 1 AND 99);
ALTER TABLE users ADD COLUMN IF NOT EXISTS device_limit_status TEXT NOT NULL DEFAULT 'none'
    CHECK (device_limit_status IN ('none', 'waiting', 'ready', 'error'));
ALTER TABLE users ADD COLUMN IF NOT EXISTS device_limit_error TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS happ_install_code TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS limited_link TEXT;

-- Keep the range synchronized when upgrading databases that used the initial
-- 1..20 device-limit constraint.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'users'::regclass AND conname = 'users_device_limit_range_check'
    ) THEN
        ALTER TABLE users DROP CONSTRAINT IF EXISTS users_device_limit_check;
        ALTER TABLE users ADD CONSTRAINT users_device_limit_range_check
            CHECK (device_limit IS NULL OR device_limit BETWEEN 1 AND 99);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS servers (
    id TEXT PRIMARY KEY,
    original_name TEXT NOT NULL,
    source_key TEXT,
    source TEXT NOT NULL DEFAULT 'unknown',
    custom_name TEXT,
    hidden BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    present_upstream BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Existing databases may have been created before provider-name identity was added.
ALTER TABLE servers ADD COLUMN IF NOT EXISTS source_key TEXT;
ALTER TABLE servers ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'unknown';
CREATE INDEX IF NOT EXISTS servers_source_key_idx ON servers(source_key);

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

-- One-time state backfill for databases that already had the original
-- device_limit/happ_install_code/limited_link columns from the reference build.
UPDATE users SET device_limit_status = CASE
    WHEN device_limit IS NULL THEN 'none'
    WHEN limited_link IS NOT NULL THEN 'ready'
    ELSE 'waiting'
END
WHERE device_limit_status = 'none' AND device_limit IS NOT NULL;
