-- ───────────────────────────────────────────────────────────────────────────
-- Migration v2: Subscriptions + Shadow fork (ghost complexes)
-- Run once: psql $DATABASE_URL -f sql/migration_v2_subscriptions_ghosts.sql
-- ───────────────────────────────────────────────────────────────────────────

-- 1. Подписки пользователей на публичные комплексы сообщества
CREATE TABLE IF NOT EXISTS complex_subscriptions (
    user_id    INTEGER NOT NULL REFERENCES users(id)     ON DELETE CASCADE,
    complex_id INTEGER NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, complex_id)
);
CREATE INDEX IF NOT EXISTS idx_csubscriptions_complex
    ON complex_subscriptions(complex_id);

-- 2. Shadow fork (ghost) — заморозка комплекса для зависимых пользователей
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS is_ghost          BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS ghost_of_id       INTEGER REFERENCES complexes(id) ON DELETE SET NULL;
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS ghost_likes_count INTEGER;
-- ghost_reason: 'edited' | 'privatized' | 'deleted'
ALTER TABLE complexes ADD COLUMN IF NOT EXISTS ghost_reason      TEXT;
