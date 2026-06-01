-- ═══════════════════════════════════════════════════════════════════════════════
-- CoI Public Planner — полный монолитный DDL (standalone, с нуля)
-- Запустить один раз на чистой БД:
--   psql -U postgres -d coi_public -f web_public/sql/schema.sql
-- ═══════════════════════════════════════════════════════════════════════════════

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. ИГРОВЫЕ ДАННЫЕ (items, buildings, recipes, resource_flows)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS items (
    id     SERIAL       PRIMARY KEY,
    name   VARCHAR(200) NOT NULL,
    po_key TEXT,                         -- msgid из .po файла игры (для локализации)
    CONSTRAINT uq_items_name UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS buildings (
    id             SERIAL       PRIMARY KEY,
    name           VARCHAR(200) NOT NULL,
    po_key         TEXT,                 -- msgid из .po файла игры (для локализации)
    workers        SMALLINT     CHECK (workers >= 0),
    electricity_kw NUMERIC(10, 2),
    footprint      VARCHAR(20),
    designation    VARCHAR(100),
    CONSTRAINT uq_buildings_name UNIQUE (name)
);

-- Переводы игрового контента из .po файлов игры
CREATE TABLE IF NOT EXISTS content_translations (
    po_key TEXT NOT NULL,
    lang   TEXT NOT NULL,
    value  TEXT NOT NULL,
    PRIMARY KEY (po_key, lang)
);

CREATE INDEX IF NOT EXISTS idx_buildings_designation ON buildings (designation);

CREATE TABLE IF NOT EXISTS building_maintenance (
    id          SERIAL  PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES buildings (id) ON DELETE CASCADE,
    item        VARCHAR(100)  NOT NULL,
    rate        NUMERIC(8, 4) NOT NULL CHECK (rate > 0),
    period_s    SMALLINT      NOT NULL CHECK (period_s > 0),
    rate_per_min NUMERIC(8, 4) NOT NULL CHECK (rate_per_min > 0),
    CONSTRAINT uq_building_maint UNIQUE (building_id, item)
);

CREATE INDEX IF NOT EXISTS idx_bld_maint_building ON building_maintenance (building_id);

CREATE TABLE IF NOT EXISTS building_construction (
    id          SERIAL  PRIMARY KEY,
    building_id INTEGER NOT NULL REFERENCES buildings (id) ON DELETE CASCADE,
    item        VARCHAR(200) NOT NULL,
    qty         SMALLINT     NOT NULL CHECK (qty > 0),
    CONSTRAINT uq_building_constr UNIQUE (building_id, item)
);

CREATE TABLE IF NOT EXISTS recipes (
    id           SERIAL  PRIMARY KEY,
    wiki_id      TEXT,                    -- стабильный ID с вики (RecipeId из Cargo API)
    machine_id   INTEGER REFERENCES buildings (id) ON DELETE SET NULL,
    machine_name VARCHAR(200) NOT NULL,
    cycle_time_s NUMERIC(8, 2) CHECK (cycle_time_s > 0),
    deprecated   BOOLEAN NOT NULL DEFAULT FALSE
);

-- wiki_id уникален в рамках одной машины (разные машины могут иметь один wiki_id)
CREATE UNIQUE INDEX IF NOT EXISTS uq_recipes_wiki_machine
    ON recipes(wiki_id, machine_id) WHERE wiki_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_recipes_machine_id   ON recipes (machine_id);
CREATE INDEX IF NOT EXISTS idx_recipes_machine_name ON recipes (machine_name);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. КОМПЛЕКСЫ
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS complexes (
    id                   SERIAL       PRIMARY KEY,
    name                 VARCHAR(200) NOT NULL,
    description          TEXT,
    total_workers        NUMERIC(12, 2),
    total_electricity_kw NUMERIC(12, 2),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
    -- user_id, visibility, slug и др. добавляются в разделе 5 (AUTH)
);

CREATE INDEX IF NOT EXISTS idx_complexes_name ON complexes (name);

-- resource_flows — создаётся после complexes (нужен FK complex_id → complexes)
CREATE TABLE IF NOT EXISTS resource_flows (
    id            SERIAL   PRIMARY KEY,
    parent_type   SMALLINT NOT NULL CHECK (parent_type IN (0, 1)),
    parent_id     INTEGER  NOT NULL,
    recipe_id     INTEGER  REFERENCES recipes   (id) ON DELETE CASCADE,
    complex_id    INTEGER  REFERENCES complexes (id) ON DELETE CASCADE,
    item_id       INTEGER  NOT NULL REFERENCES items (id),
    direction     SMALLINT NOT NULL CHECK (direction IN (0, 1)),
    qty_per_cycle SMALLINT       CHECK (qty_per_cycle > 0),
    qty_per_min   NUMERIC(12, 4) CHECK (qty_per_min   > 0),
    sort_order    SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT chk_flow_parent CHECK (
        (parent_type = 0
            AND recipe_id  IS NOT NULL AND recipe_id  = parent_id AND complex_id IS NULL)
        OR
        (parent_type = 1
            AND complex_id IS NOT NULL AND complex_id = parent_id AND recipe_id  IS NULL)
    ),
    CONSTRAINT uq_resource_flow UNIQUE (parent_type, parent_id, item_id, direction)
);

CREATE INDEX IF NOT EXISTS idx_rf_parent     ON resource_flows (parent_type, parent_id);
CREATE INDEX IF NOT EXISTS idx_rf_recipe     ON resource_flows (recipe_id);
CREATE INDEX IF NOT EXISTS idx_rf_complex    ON resource_flows (complex_id);
CREATE INDEX IF NOT EXISTS idx_rf_item       ON resource_flows (item_id);
CREATE INDEX IF NOT EXISTS idx_rf_parent_dir ON resource_flows (parent_type, parent_id, direction);
CREATE INDEX IF NOT EXISTS idx_rf_item_dir   ON resource_flows (item_id, direction);

-- complex_members: узлы графа (рецепты или подкомплексы)
CREATE TABLE IF NOT EXISTS complex_members (
    id               SERIAL   PRIMARY KEY,
    complex_id       INTEGER  NOT NULL REFERENCES complexes (id) ON DELETE CASCADE,
    child_type       SMALLINT NOT NULL CHECK (child_type IN (0, 1)),
    child_id         INTEGER  NOT NULL,
    recipe_id        INTEGER  REFERENCES recipes   (id) ON DELETE CASCADE,
    child_complex_id INTEGER  REFERENCES complexes (id) ON DELETE CASCADE,
    multiplier       NUMERIC(10, 4) NOT NULL DEFAULT 1 CHECK (multiplier > 0),
    -- Позиция и состояние узла на холсте
    pos_x            INTEGER  NOT NULL DEFAULT 0,
    pos_y            INTEGER  NOT NULL DEFAULT 0,
    efficiency       NUMERIC(6, 4)   NOT NULL DEFAULT 1.0,
    idle_item        TEXT,
    idle_direction   SMALLINT,
    is_manual_partial BOOLEAN NOT NULL DEFAULT FALSE,
    external_ports   TEXT,            -- JSON-массив [{item, direction}] — явно помеченные внешние порты
    CONSTRAINT chk_member_refs CHECK (
        (child_type = 0
            AND recipe_id        IS NOT NULL AND recipe_id        = child_id AND child_complex_id IS NULL)
        OR
        (child_type = 1
            AND child_complex_id IS NOT NULL AND child_complex_id = child_id AND recipe_id        IS NULL)
    ),
    CONSTRAINT no_self_reference CHECK (child_type = 0 OR complex_id <> child_id),
    CONSTRAINT uq_complex_member UNIQUE (complex_id, child_type, child_id)
);

CREATE INDEX IF NOT EXISTS idx_cx_members_complex       ON complex_members (complex_id);
CREATE INDEX IF NOT EXISTS idx_cx_members_recipe        ON complex_members (recipe_id);
CREATE INDEX IF NOT EXISTS idx_cx_members_child_complex ON complex_members (child_complex_id);

-- complex_edges: рёбра графа (соединения между узлами)
CREATE TABLE IF NOT EXISTS complex_edges (
    id             SERIAL  PRIMARY KEY,
    complex_id     INTEGER NOT NULL REFERENCES complexes       (id) ON DELETE CASCADE,
    from_member_id INTEGER NOT NULL REFERENCES complex_members (id) ON DELETE CASCADE,
    to_member_id   INTEGER NOT NULL REFERENCES complex_members (id) ON DELETE CASCADE,
    resource_item  TEXT    NOT NULL,
    lcm_mode       BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_cx_edges_complex  ON complex_edges (complex_id);
CREATE INDEX IF NOT EXISTS idx_cx_edges_from     ON complex_edges (from_member_id);
CREATE INDEX IF NOT EXISTS idx_cx_edges_to       ON complex_edges (to_member_id);

-- complex_maintenance: агрегированный расход запчастей
CREATE TABLE IF NOT EXISTS complex_maintenance (
    id           SERIAL  PRIMARY KEY,
    complex_id   INTEGER NOT NULL REFERENCES complexes (id) ON DELETE CASCADE,
    item         VARCHAR(100)  NOT NULL,
    rate_per_min NUMERIC(12, 4) NOT NULL CHECK (rate_per_min > 0),
    CONSTRAINT uq_complex_maint UNIQUE (complex_id, item)
);

CREATE INDEX IF NOT EXISTS idx_cx_maint_complex ON complex_maintenance (complex_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. ТРИГГЕРЫ И ФУНКЦИИ (комплексы)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION fn_check_complex_cycle()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.child_type <> 1 THEN RETURN NEW; END IF;
    IF EXISTS (
        WITH RECURSIVE descendants AS (
            SELECT NEW.child_id AS cid
            UNION ALL
            SELECT cm.child_id FROM descendants d JOIN complex_members cm ON cm.complex_id = d.cid
            WHERE cm.child_type = 1
        )
        SELECT 1 FROM descendants WHERE cid = NEW.complex_id
    ) THEN
        RAISE EXCEPTION 'Cycle detected: complex % is already a descendant of complex %',
            NEW.complex_id, NEW.child_id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_no_complex_cycle ON complex_members;
CREATE TRIGGER trg_no_complex_cycle
BEFORE INSERT OR UPDATE ON complex_members
FOR EACH ROW EXECUTE FUNCTION fn_check_complex_cycle();

CREATE OR REPLACE FUNCTION fn_set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$;

DROP TRIGGER IF EXISTS trg_complexes_updated_at ON complexes;
CREATE TRIGGER trg_complexes_updated_at
BEFORE UPDATE ON complexes
FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE OR REPLACE FUNCTION recalculate_complex(p_complex_id INTEGER)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE v_workers NUMERIC(12,2); v_electricity NUMERIC(12,2);
BEGIN
    DELETE FROM resource_flows WHERE parent_type = 1 AND parent_id = p_complex_id;
    INSERT INTO resource_flows (parent_type, parent_id, complex_id, item_id, direction, qty_per_min)
    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id AS cid, 1.0::NUMERIC(12,4) AS eff_mult
        UNION ALL
        SELECT cm.child_id, (t.eff_mult * cm.multiplier)::NUMERIC(12,4)
        FROM cx_tree t JOIN complex_members cm ON cm.complex_id = t.cid WHERE cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id, SUM(t.eff_mult * cm.multiplier) AS total_mult
        FROM cx_tree t JOIN complex_members cm ON cm.complex_id = t.cid WHERE cm.child_type = 0
        GROUP BY cm.recipe_id
    ),
    resource_flow AS (
        SELECT rf.item_id,
            SUM(CASE rf.direction WHEN 1 THEN ar.total_mult * rf.qty_per_min
                                  WHEN 0 THEN -ar.total_mult * rf.qty_per_min END) AS net_qty
        FROM all_recipes ar JOIN resource_flows rf ON rf.parent_type = 0 AND rf.recipe_id = ar.recipe_id
        WHERE rf.qty_per_min IS NOT NULL GROUP BY rf.item_id
    )
    SELECT 1, p_complex_id, p_complex_id, item_id,
           CASE WHEN net_qty > 0 THEN 1 ELSE 0 END, ABS(net_qty)
    FROM resource_flow WHERE net_qty <> 0;

    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id AS cid, 1.0::NUMERIC(12,4) AS eff_mult
        UNION ALL
        SELECT cm.child_id, (t.eff_mult * cm.multiplier)::NUMERIC(12,4)
        FROM cx_tree t JOIN complex_members cm ON cm.complex_id = t.cid WHERE cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id, SUM(t.eff_mult * cm.multiplier) AS total_mult
        FROM cx_tree t JOIN complex_members cm ON cm.complex_id = t.cid WHERE cm.child_type = 0
        GROUP BY cm.recipe_id
    )
    SELECT COALESCE(SUM(ar.total_mult * COALESCE(b.workers, 0)), 0),
           COALESCE(SUM(ar.total_mult * COALESCE(b.electricity_kw, 0)), 0)
    INTO v_workers, v_electricity
    FROM all_recipes ar JOIN recipes r ON r.id = ar.recipe_id LEFT JOIN buildings b ON b.id = r.machine_id;

    UPDATE complexes SET total_workers = v_workers, total_electricity_kw = v_electricity WHERE id = p_complex_id;

    DELETE FROM complex_maintenance WHERE complex_id = p_complex_id;
    INSERT INTO complex_maintenance (complex_id, item, rate_per_min)
    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id AS cid, 1.0::NUMERIC(12,4) AS eff_mult
        UNION ALL
        SELECT cm.child_id, (t.eff_mult * cm.multiplier)::NUMERIC(12,4)
        FROM cx_tree t JOIN complex_members cm ON cm.complex_id = t.cid WHERE cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id, SUM(t.eff_mult * cm.multiplier) AS total_mult
        FROM cx_tree t JOIN complex_members cm ON cm.complex_id = t.cid WHERE cm.child_type = 0
        GROUP BY cm.recipe_id
    )
    SELECT p_complex_id, bm.item, SUM(ar.total_mult * bm.rate_per_min)
    FROM all_recipes ar JOIN recipes r ON r.id = ar.recipe_id
    JOIN building_maintenance bm ON bm.building_id = r.machine_id GROUP BY bm.item;
END;
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. КОНТРАКТЫ (торговля ресурсами через биржу)
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS contracts (
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(200) NOT NULL,
    contract_type VARCHAR(50),
    unity_per_month INTEGER,
    duration_months INTEGER
);

CREATE TABLE IF NOT EXISTS contract_items (
    id          SERIAL  PRIMARY KEY,
    contract_id INTEGER NOT NULL REFERENCES contracts (id) ON DELETE CASCADE,
    item_name   VARCHAR(200) NOT NULL,
    direction   SMALLINT NOT NULL CHECK (direction IN (0, 1)),  -- 0=buy, 1=sell
    qty_per_month NUMERIC(12, 4),
    price_unity   NUMERIC(12, 4)
);

CREATE INDEX IF NOT EXISTS idx_contract_items_contract ON contract_items (contract_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 5. АВТОРИЗАЦИЯ И ПОЛЬЗОВАТЕЛИ
-- ─────────────────────────────────────────────────────────────────────────────

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'auth_provider') THEN
        CREATE TYPE auth_provider AS ENUM ('google', 'steam', 'session_code', 'guest', 'email');
    ELSE
        BEGIN ALTER TYPE auth_provider ADD VALUE IF NOT EXISTS 'guest'; EXCEPTION WHEN others THEN END;
        BEGIN ALTER TYPE auth_provider ADD VALUE IF NOT EXISTS 'email'; EXCEPTION WHEN others THEN END;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS users (
    id               SERIAL        PRIMARY KEY,
    provider         auth_provider NOT NULL,
    provider_user_id TEXT          NOT NULL,
    display_name     TEXT          NOT NULL,
    avatar_url       TEXT,
    email            TEXT,
    is_premium       BOOLEAN       NOT NULL DEFAULT FALSE,
    is_guest         BOOLEAN       NOT NULL DEFAULT FALSE,
    guest_cookie     UUID,
    created_at       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    last_seen_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    UNIQUE (provider, provider_user_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_guest_cookie
    ON users (guest_cookie) WHERE guest_cookie IS NOT NULL;

CREATE TABLE IF NOT EXISTS session_codes (
    id           SERIAL      PRIMARY KEY,
    user_id      INT         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code         CHAR(8)     NOT NULL UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

-- Одноразовые коды для email-авторизации (TTL 15 минут)
CREATE TABLE IF NOT EXISTS email_codes (
    id         SERIAL      PRIMARY KEY,
    email      TEXT        NOT NULL,
    code       CHAR(6)     NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '15 minutes',
    used_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_email_codes_email ON email_codes(email);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id INT  NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS user_recipe_prefs (
    user_id   INT     NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    recipe_id INT     NOT NULL REFERENCES recipes(id)  ON DELETE CASCADE,
    hidden    BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (user_id, recipe_id)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- 6. РАСШИРЕНИЕ ТАБЛИЦЫ COMPLEXES (публичные колонки)
-- ─────────────────────────────────────────────────────────────────────────────

ALTER TABLE complexes
    ADD COLUMN IF NOT EXISTS user_id        INT        REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS visibility     TEXT       NOT NULL DEFAULT 'private'
        CHECK (visibility IN ('private', 'public')),
    ADD COLUMN IF NOT EXISTS forked_from_id INT        REFERENCES complexes(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS slug           UUID       NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN IF NOT EXISTS likes_count    INT        NOT NULL DEFAULT 0;

-- visibility ENUM (если тип уже создан — используем его, иначе TEXT CHECK достаточно)
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'visibility') THEN
        CREATE TYPE visibility AS ENUM ('private', 'public');
    END IF;
END $$;

-- Per-user unique complex name (не глобальный)
ALTER TABLE complexes DROP CONSTRAINT IF EXISTS uq_complexes_name;
CREATE UNIQUE INDEX IF NOT EXISTS uq_complexes_user_name
    ON complexes(user_id, name) WHERE user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_complexes_user    ON complexes(user_id);
CREATE INDEX IF NOT EXISTS idx_complexes_visible ON complexes(visibility) WHERE visibility = 'public';
CREATE UNIQUE INDEX IF NOT EXISTS idx_complexes_slug ON complexes(slug);

-- ─────────────────────────────────────────────────────────────────────────────
-- 7. ЛАЙКИ
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS complex_likes (
    user_id    INT NOT NULL REFERENCES users(id)     ON DELETE CASCADE,
    complex_id INT NOT NULL REFERENCES complexes(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, complex_id)
);

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
-- 8. ПРЕДСТАВЛЕНИЯ
-- ─────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW v_recipes_full AS
SELECT r.id AS recipe_id, r.machine_name, r.cycle_time_s,
    json_agg(json_build_object('item', i.name, 'qty_per_cycle', rf.qty_per_cycle, 'qty_per_min', rf.qty_per_min)
        ORDER BY rf.sort_order) FILTER (WHERE rf.direction = 0) AS inputs,
    json_agg(json_build_object('item', i.name, 'qty_per_cycle', rf.qty_per_cycle, 'qty_per_min', rf.qty_per_min)
        ORDER BY rf.sort_order) FILTER (WHERE rf.direction = 1) AS outputs
FROM recipes r
LEFT JOIN resource_flows rf ON rf.parent_type = 0 AND rf.recipe_id = r.id
LEFT JOIN items          i  ON i.id = rf.item_id
GROUP BY r.id, r.machine_name, r.cycle_time_s;

-- ─────────────────────────────────────────────────────────────────────────────

DO $$
BEGIN RAISE NOTICE 'CoI Public schema initialization complete.'; END $$;
