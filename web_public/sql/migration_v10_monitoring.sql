-- Migration v10: мониторинг активности.
--
-- Два хранилища:
--   daily_stats    — по одному срезу на сутки: пользователи, комплексы, здоровье;
--   monitor_alerts — сработавшие проверки, чтобы не слать одно письмо дважды.
--
-- Зачем срезы, если почти всё считается из created_at: cleanup-guests удаляет
-- гостей старше полугода, и история трафика вместе с ними исчезает. Плюс размер
-- БД, свободное место и «активные за сутки» задним числом не восстановить.
--
-- Идемпотентно.

CREATE TABLE IF NOT EXISTS daily_stats (
    -- Сутки, за которые собран срез (UTC). Первичный ключ: один день — одна строка.
    day                 DATE        PRIMARY KEY,
    collected_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- TRUE, если строка восстановлена задним числом из created_at.
    -- У таких строк поля здоровья и активности пустые — их взять неоткуда.
    is_backfilled       BOOLEAN     NOT NULL DEFAULT FALSE,

    -- ── Пользователи: состояние на конец суток ──────────────────────
    users_total         INTEGER,
    users_registered    INTEGER,
    users_guests        INTEGER,
    -- ── Пользователи: прирост за сутки ──────────────────────────────
    new_guests          INTEGER,
    new_registered      INTEGER,
    -- ── Активность (не восстанавливается задним числом) ─────────────
    active_24h          INTEGER,
    active_7d           INTEGER,

    -- ── Комплексы: состояние на конец суток ─────────────────────────
    cx_total            INTEGER,
    cx_public           INTEGER,
    -- ── Комплексы: прирост за сутки ─────────────────────────────────
    cx_new              INTEGER,
    cx_new_public       INTEGER,
    cx_new_forks        INTEGER,
    cx_edited           INTEGER,
    likes_new           INTEGER,
    subs_new            INTEGER,

    -- ── Здоровье (только на живом сборе) ────────────────────────────
    db_size_bytes       BIGINT,
    disk_free_bytes     BIGINT,
    disk_total_bytes    BIGINT,
    db_connections      INTEGER,
    collect_ms          INTEGER,

    -- Разрезы, которые неудобно держать колонками: провайдеры входа,
    -- utm_source, рефереры, топ комплексов. Схема внутри — см. monitoring.py.
    breakdown           JSONB       NOT NULL DEFAULT '{}'::jsonb
);

COMMENT ON TABLE daily_stats IS 'Суточные срезы активности и здоровья; заполняет flask collect-stats';

CREATE INDEX IF NOT EXISTS idx_daily_stats_day_desc ON daily_stats (day DESC);


CREATE TABLE IF NOT EXISTS monitor_alerts (
    id          SERIAL      PRIMARY KEY,
    day         DATE        NOT NULL,
    -- Тип проверки: disk_low, db_growth, traffic_drop, no_traffic, collector_gap.
    kind        TEXT        NOT NULL,
    severity    TEXT        NOT NULL DEFAULT 'warning'
                            CHECK (severity IN ('info', 'warning', 'critical')),
    message     TEXT        NOT NULL,
    -- Когда ушло письмо. NULL — проверка сработала, но письмо не отправлено
    -- (SMTP не настроен или отправка упала).
    notified_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Один тип проверки на одни сутки: повторный сбор не плодит письма.
    UNIQUE (day, kind)
);

COMMENT ON TABLE monitor_alerts IS 'Сработавшие проверки мониторинга; UNIQUE(day, kind) гасит повторы';

CREATE INDEX IF NOT EXISTS idx_monitor_alerts_created ON monitor_alerts (created_at DESC);


DO $$
BEGIN RAISE NOTICE 'Migration v10 (monitoring) complete.'; END $$;
