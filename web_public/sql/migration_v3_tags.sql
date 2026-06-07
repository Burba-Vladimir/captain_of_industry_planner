-- ───────────────────────────────────────────────────────────────────────────
-- Migration v3: Hashtags for complexes
-- Run once: psql $DATABASE_URL -f sql/migration_v3_tags.sql
-- ───────────────────────────────────────────────────────────────────────────

-- 1. Глобальный справочник тегов (lowercase, alphanumeric + hyphens)
CREATE TABLE IF NOT EXISTS tags (
    id   SERIAL      PRIMARY KEY,
    name VARCHAR(30) UNIQUE NOT NULL
);
-- Индекс для autocomplete (prefix-match)
CREATE INDEX IF NOT EXISTS idx_tags_name_prefix ON tags(name text_pattern_ops);

-- 2. Привязка тегов к комплексам
CREATE TABLE IF NOT EXISTS complex_tags (
    complex_id INTEGER NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES tags(id)     ON DELETE CASCADE,
    PRIMARY KEY (complex_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_complex_tags_tag     ON complex_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_complex_tags_complex ON complex_tags(complex_id);
