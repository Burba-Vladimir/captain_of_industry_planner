-- ═══════════════════════════════════════════════════════════════════════════════
-- CoI Public Schema — Full database setup
-- Run once on new PostgreSQL database: psql -U postgres -d coi_public -f sql/schema.sql
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. AUTHENTICATION & USERS
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TYPE auth_provider AS ENUM ('google', 'steam', 'session_code', 'guest');

CREATE TABLE users (
    id               SERIAL       PRIMARY KEY,
    provider         auth_provider NOT NULL,
    provider_user_id TEXT         NOT NULL,
    display_name     TEXT         NOT NULL,
    avatar_url       TEXT,
    email            TEXT,
    is_premium       BOOLEAN      NOT NULL DEFAULT FALSE,
    is_guest         BOOLEAN      NOT NULL DEFAULT FALSE,
    guest_cookie     UUID,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (provider, provider_user_id)
);

CREATE UNIQUE INDEX idx_users_guest_cookie
    ON users (guest_cookie) WHERE guest_cookie IS NOT NULL;

-- Session codes (fallback authentication without OAuth)
CREATE TABLE session_codes (
    id         SERIAL      PRIMARY KEY,
    user_id    INT         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code       CHAR(8)     NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

-- User settings (key-value store for flexibility)
CREATE TABLE user_settings (
    user_id    INT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
-- Keys: show_public_complexes, ui_language, ui_theme

-- Per-user recipe preferences (replaces global deprecated flag)
CREATE TABLE user_recipe_prefs (
    user_id   INT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipe_id INT  NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    hidden    BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (user_id, recipe_id)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. COMPLEXES — PUBLIC / VISIBILITY / SHARING
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TYPE visibility AS ENUM ('private', 'public');

-- Add public-specific columns to complexes (assumes complexes table exists from local version)
ALTER TABLE complexes
    ADD COLUMN IF NOT EXISTS user_id        INT         REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS visibility     visibility  NOT NULL DEFAULT 'private',
    ADD COLUMN IF NOT EXISTS forked_from_id INT         REFERENCES complexes(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS description    TEXT,
    ADD COLUMN IF NOT EXISTS slug           UUID        NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN IF NOT EXISTS likes_count    INT         NOT NULL DEFAULT 0;

-- Per-user unique complex names (different users can have complexes with same name)
ALTER TABLE complexes DROP CONSTRAINT IF EXISTS uq_complexes_name;
CREATE UNIQUE INDEX IF NOT EXISTS uq_complexes_user_name
    ON complexes(user_id, name) WHERE user_id IS NOT NULL;

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_complexes_user    ON complexes(user_id);
CREATE INDEX IF NOT EXISTS idx_complexes_visible ON complexes(visibility) WHERE visibility = 'public';
CREATE UNIQUE INDEX IF NOT EXISTS idx_complexes_slug ON complexes(slug);

-- Likes / ratings
CREATE TABLE IF NOT EXISTS complex_likes (
    user_id    INT NOT NULL REFERENCES users(id)     ON DELETE CASCADE,
    complex_id INT NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, complex_id)
);

-- Trigger to update likes_count on complex_likes changes
CREATE OR REPLACE FUNCTION update_likes_count()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE complexes SET likes_count = likes_count + 1 WHERE id = NEW.complex_id;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE complexes SET likes_count = GREATEST(0, likes_count - 1) WHERE id = OLD.complex_id;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_likes_count ON complex_likes;
CREATE TRIGGER trg_likes_count
AFTER INSERT OR DELETE ON complex_likes
FOR EACH ROW EXECUTE FUNCTION update_likes_count();

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. HOUSEKEEPING
-- ─────────────────────────────────────────────────────────────────────────────

-- Update timestamp function (used by triggers)
CREATE OR REPLACE FUNCTION fn_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

-- Apply updated_at trigger to complexes if not already exists
DROP TRIGGER IF EXISTS trg_complexes_updated_at ON complexes;
CREATE TRIGGER trg_complexes_updated_at
BEFORE UPDATE ON complexes
FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

DO $$
BEGIN
    RAISE NOTICE 'Schema initialization complete.';
END $$;
