-- Migration v8: атрибуция трафика — сохраняем источник перехода на первом визите.
-- utm_source берётся из ?utm_source=... (метки в публикуемых ссылках),
-- referrer — из заголовка Referer (откуда пришёл).
-- Идемпотентно.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS utm_source TEXT,
    ADD COLUMN IF NOT EXISTS referrer   TEXT;

CREATE INDEX IF NOT EXISTS idx_users_created_at ON users (created_at);
CREATE INDEX IF NOT EXISTS idx_users_utm_source ON users (utm_source) WHERE utm_source IS NOT NULL;
