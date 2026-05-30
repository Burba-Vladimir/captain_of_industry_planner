-- =============================================================
-- Captain of Industry — инкрементальная миграция на v2
-- =============================================================
-- Что делает скрипт:
--   1. recipes        — добавляет колонку deprecated
--   2. resource_flows — создаёт таблицу, переносит данные из
--                       recipe_items (если та ещё существует),
--                       удаляет recipe_items
--   3. complexes      — создаёт таблицу
--   4. resource_flows — добавляет FK → complexes
--   5. complex_members, complex_maintenance — создаёт таблицы
--   6. Пересоздаёт v_recipes_full (теперь читает resource_flows)
--   7. Создаёт v_complexes_full
--   8. Создаёт триггеры и функцию recalculate_complex
--
-- Идемпотентен: повторный запуск безопасен.
-- =============================================================

BEGIN;

-- -------------------------------------------------------------
-- 1. recipes: колонка deprecated
-- -------------------------------------------------------------

ALTER TABLE recipes
    ADD COLUMN IF NOT EXISTS deprecated BOOLEAN NOT NULL DEFAULT FALSE;


-- -------------------------------------------------------------
-- 2. resource_flows: создать таблицу
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS resource_flows (
    id            SERIAL   PRIMARY KEY,

    parent_type   SMALLINT NOT NULL CHECK (parent_type IN (0, 1)),
    parent_id     INTEGER  NOT NULL,
    recipe_id     INTEGER  REFERENCES recipes (id) ON DELETE CASCADE,
    complex_id    INTEGER,  -- FK → complexes добавляется ниже

    item_id       INTEGER  NOT NULL REFERENCES items (id),
    direction     SMALLINT NOT NULL CHECK (direction IN (0, 1)),

    qty_per_cycle SMALLINT       CHECK (qty_per_cycle > 0),
    qty_per_min   NUMERIC(12, 4) CHECK (qty_per_min   > 0),
    sort_order    SMALLINT NOT NULL DEFAULT 0,

    CONSTRAINT chk_flow_parent CHECK (
        (parent_type = 0
            AND recipe_id  IS NOT NULL
            AND recipe_id  =  parent_id
            AND complex_id IS NULL)
        OR
        (parent_type = 1
            AND complex_id IS NOT NULL
            AND complex_id =  parent_id
            AND recipe_id  IS NULL)
    ),

    CONSTRAINT uq_resource_flow UNIQUE (parent_type, parent_id, item_id, direction)
);

COMMENT ON TABLE  resource_flows               IS 'Потоки ресурсов: parent_type 0=рецепт, 1=комплекс; direction 0=вход, 1=выход';
COMMENT ON COLUMN resource_flows.parent_type   IS '0 = рецепт, 1 = комплекс';
COMMENT ON COLUMN resource_flows.parent_id     IS 'Денормализованный ID без FK — удобен в запросах';
COMMENT ON COLUMN resource_flows.recipe_id     IS 'FK на recipes; заполнен только при parent_type = 0';
COMMENT ON COLUMN resource_flows.complex_id    IS 'FK на complexes; заполнен только при parent_type = 1';
COMMENT ON COLUMN resource_flows.direction     IS '0 = потребляется (input), 1 = производится (output)';
COMMENT ON COLUMN resource_flows.qty_per_cycle IS 'Количество за цикл — только для рецептов';
COMMENT ON COLUMN resource_flows.qty_per_min   IS 'Количество в минуту; для комплексов — нетто-агрегат';

CREATE INDEX IF NOT EXISTS idx_rf_parent     ON resource_flows (parent_type, parent_id);
CREATE INDEX IF NOT EXISTS idx_rf_recipe     ON resource_flows (recipe_id);
CREATE INDEX IF NOT EXISTS idx_rf_complex    ON resource_flows (complex_id);
CREATE INDEX IF NOT EXISTS idx_rf_item       ON resource_flows (item_id);
CREATE INDEX IF NOT EXISTS idx_rf_parent_dir ON resource_flows (parent_type, parent_id, direction);
CREATE INDEX IF NOT EXISTS idx_rf_item_dir   ON resource_flows (item_id, direction);


-- -------------------------------------------------------------
-- 3. Перенести данные из recipe_items → resource_flows,
--    затем удалить recipe_items
-- -------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE  table_schema = 'public'
          AND  table_name   = 'recipe_items'
    ) THEN
        RAISE NOTICE 'recipe_items found — migrating to resource_flows...';

        INSERT INTO resource_flows
            (parent_type, parent_id, recipe_id,
             item_id, direction, qty_per_cycle, qty_per_min, sort_order)
        SELECT
            0,          -- parent_type = recipe
            recipe_id,  -- parent_id
            recipe_id,  -- FK
            item_id,
            direction,
            qty_per_cycle,
            qty_per_min,
            sort_order
        FROM recipe_items
        ON CONFLICT (parent_type, parent_id, item_id, direction) DO NOTHING;

        DROP TABLE recipe_items;
        RAISE NOTICE 'recipe_items migrated and dropped.';
    ELSE
        RAISE NOTICE 'recipe_items not found — skipping migration.';
    END IF;
END;
$$;


-- -------------------------------------------------------------
-- 4. complexes
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS complexes (
    id                   SERIAL       PRIMARY KEY,
    name                 VARCHAR(200) NOT NULL,
    description          TEXT,
    total_workers        NUMERIC(12, 2),
    total_electricity_kw NUMERIC(12, 2),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_complexes_name UNIQUE (name)
);

COMMENT ON TABLE  complexes                      IS 'Пользовательские комплексы — группы рецептов и подкомплексов';
COMMENT ON COLUMN complexes.total_workers        IS 'Суммарное число рабочих с учётом всех мультипликаторов';
COMMENT ON COLUMN complexes.total_electricity_kw IS 'Суммарное электричество (нетто): > 0 потребление, < 0 выработка';

CREATE INDEX IF NOT EXISTS idx_complexes_name ON complexes (name);


-- -------------------------------------------------------------
-- 5. resource_flows: FK → complexes (если ещё нет)
-- -------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE  conname = 'fk_resource_flows_complex'
    ) THEN
        ALTER TABLE resource_flows
            ADD CONSTRAINT fk_resource_flows_complex
            FOREIGN KEY (complex_id) REFERENCES complexes (id) ON DELETE CASCADE;
        RAISE NOTICE 'FK fk_resource_flows_complex added.';
    ELSE
        RAISE NOTICE 'FK fk_resource_flows_complex already exists — skipping.';
    END IF;
END;
$$;


-- -------------------------------------------------------------
-- 6. complex_members
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS complex_members (
    id               SERIAL   PRIMARY KEY,
    complex_id       INTEGER  NOT NULL REFERENCES complexes (id) ON DELETE CASCADE,
    child_type       SMALLINT NOT NULL CHECK (child_type IN (0, 1)),
    child_id         INTEGER  NOT NULL,
    recipe_id        INTEGER  REFERENCES recipes  (id) ON DELETE CASCADE,
    child_complex_id INTEGER  REFERENCES complexes(id) ON DELETE CASCADE,
    multiplier       NUMERIC(10, 4) NOT NULL DEFAULT 1 CHECK (multiplier > 0),

    CONSTRAINT chk_member_refs CHECK (
        (child_type = 0
            AND recipe_id        IS NOT NULL
            AND recipe_id        =  child_id
            AND child_complex_id IS NULL)
        OR
        (child_type = 1
            AND child_complex_id IS NOT NULL
            AND child_complex_id =  child_id
            AND recipe_id        IS NULL)
    ),
    CONSTRAINT no_self_reference CHECK (child_type = 0 OR complex_id <> child_id),
    CONSTRAINT uq_complex_member UNIQUE (complex_id, child_type, child_id)
);

COMMENT ON TABLE  complex_members                  IS 'Члены комплекса: рецепты (child_type=0) и подкомплексы (child_type=1)';
COMMENT ON COLUMN complex_members.child_type       IS '0 = рецепт, 1 = подкомплекс';
COMMENT ON COLUMN complex_members.child_id         IS 'Денормализованный ID без FK — для удобства запросов';
COMMENT ON COLUMN complex_members.recipe_id        IS 'FK на recipes; заполнен только при child_type = 0';
COMMENT ON COLUMN complex_members.child_complex_id IS 'FK на complexes; заполнен только при child_type = 1';
COMMENT ON COLUMN complex_members.multiplier       IS 'Сколько экземпляров рецепта/подкомплекса работает в составе комплекса';

CREATE INDEX IF NOT EXISTS idx_cx_members_complex       ON complex_members (complex_id);
CREATE INDEX IF NOT EXISTS idx_cx_members_recipe        ON complex_members (recipe_id);
CREATE INDEX IF NOT EXISTS idx_cx_members_child_complex ON complex_members (child_complex_id);
CREATE INDEX IF NOT EXISTS idx_cx_members_type_child    ON complex_members (complex_id, child_type);


-- -------------------------------------------------------------
-- 7. complex_maintenance
-- -------------------------------------------------------------

CREATE TABLE IF NOT EXISTS complex_maintenance (
    id           SERIAL  PRIMARY KEY,
    complex_id   INTEGER NOT NULL REFERENCES complexes (id) ON DELETE CASCADE,
    item         VARCHAR(100) NOT NULL,
    rate_per_min NUMERIC(12, 4) NOT NULL CHECK (rate_per_min > 0),

    CONSTRAINT uq_complex_maint UNIQUE (complex_id, item)
);

COMMENT ON TABLE  complex_maintenance              IS 'Суммарный расход запчастей на обслуживание всего комплекса';
COMMENT ON COLUMN complex_maintenance.item         IS 'Тип запчастей: Maintenance I / II / III';
COMMENT ON COLUMN complex_maintenance.rate_per_min IS 'Расход в минуту с учётом всех мультипликаторов';

CREATE INDEX IF NOT EXISTS idx_cx_maint_complex ON complex_maintenance (complex_id);


-- -------------------------------------------------------------
-- 8. v_recipes_full — пересоздать (теперь читает resource_flows)
-- -------------------------------------------------------------

CREATE OR REPLACE VIEW v_recipes_full AS
SELECT
    r.id          AS recipe_id,
    r.machine_name,
    r.cycle_time_s,
    json_agg(
        json_build_object(
            'item',          i.name,
            'qty_per_cycle', rf.qty_per_cycle,
            'qty_per_min',   rf.qty_per_min
        )
        ORDER BY rf.sort_order
    ) FILTER (WHERE rf.direction = 0)  AS inputs,
    json_agg(
        json_build_object(
            'item',          i.name,
            'qty_per_cycle', rf.qty_per_cycle,
            'qty_per_min',   rf.qty_per_min
        )
        ORDER BY rf.sort_order
    ) FILTER (WHERE rf.direction = 1)  AS outputs
FROM recipes r
LEFT JOIN resource_flows rf ON rf.parent_type = 0 AND rf.recipe_id = r.id
LEFT JOIN items          i  ON i.id = rf.item_id
GROUP BY r.id, r.machine_name, r.cycle_time_s;

COMMENT ON VIEW v_recipes_full IS 'Рецепты с inputs/outputs в виде JSON-агрегатов';


-- -------------------------------------------------------------
-- 9. v_complexes_full — создать
-- -------------------------------------------------------------

CREATE OR REPLACE VIEW v_complexes_full AS
SELECT
    c.id,
    c.name,
    c.description,
    c.total_workers,
    c.total_electricity_kw,
    c.updated_at,
    (SELECT json_agg(
                json_build_object('item', cm.item, 'rate_per_min', cm.rate_per_min)
                ORDER BY cm.item
            )
     FROM   complex_maintenance cm
     WHERE  cm.complex_id = c.id
    ) AS maintenance,
    (SELECT json_agg(
                json_build_object('item', i.name, 'qty_per_min', rf.qty_per_min)
                ORDER BY i.name
            )
     FROM   resource_flows rf
     JOIN   items i ON i.id = rf.item_id
     WHERE  rf.parent_type = 1 AND rf.complex_id = c.id AND rf.direction = 0
    ) AS inputs,
    (SELECT json_agg(
                json_build_object('item', i.name, 'qty_per_min', rf.qty_per_min)
                ORDER BY i.name
            )
     FROM   resource_flows rf
     JOIN   items i ON i.id = rf.item_id
     WHERE  rf.parent_type = 1 AND rf.complex_id = c.id AND rf.direction = 1
    ) AS outputs
FROM complexes c;

COMMENT ON VIEW v_complexes_full IS
'Комплекс с агрегированными данными из resource_flows (parent_type=1)';


-- -------------------------------------------------------------
-- 10. Триггерная функция: запрет циклов
-- -------------------------------------------------------------

CREATE OR REPLACE FUNCTION fn_check_complex_cycle()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.child_type <> 1 THEN
        RETURN NEW;
    END IF;
    IF EXISTS (
        WITH RECURSIVE descendants AS (
            SELECT NEW.child_id AS cid
            UNION ALL
            SELECT cm.child_id
            FROM   descendants     d
            JOIN   complex_members cm ON cm.complex_id = d.cid
            WHERE  cm.child_type = 1
        )
        SELECT 1 FROM descendants WHERE cid = NEW.complex_id
    ) THEN
        RAISE EXCEPTION
            'Cycle detected: complex % is already a descendant of complex %',
            NEW.complex_id, NEW.child_id;
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_no_complex_cycle'
    ) THEN
        CREATE TRIGGER trg_no_complex_cycle
        BEFORE INSERT OR UPDATE ON complex_members
        FOR EACH ROW EXECUTE FUNCTION fn_check_complex_cycle();
    END IF;
END;
$$;


-- -------------------------------------------------------------
-- 11. Триггерная функция: updated_at
-- -------------------------------------------------------------

CREATE OR REPLACE FUNCTION fn_set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_complexes_updated_at'
    ) THEN
        CREATE TRIGGER trg_complexes_updated_at
        BEFORE UPDATE ON complexes
        FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();
    END IF;
END;
$$;


-- -------------------------------------------------------------
-- 12. recalculate_complex
-- -------------------------------------------------------------

CREATE OR REPLACE FUNCTION recalculate_complex(p_complex_id INTEGER)
RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE
    v_workers     NUMERIC(12, 2);
    v_electricity NUMERIC(12, 2);
BEGIN
    -- Нетто-ресурсы
    DELETE FROM resource_flows
    WHERE  parent_type = 1 AND parent_id = p_complex_id;

    INSERT INTO resource_flows
        (parent_type, parent_id, complex_id, item_id, direction, qty_per_min)
    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id AS cid, 1.0::NUMERIC(12, 4) AS eff_mult
        UNION ALL
        SELECT cm.child_id, t.eff_mult * cm.multiplier
        FROM   cx_tree t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id, SUM(t.eff_mult * cm.multiplier) AS total_mult
        FROM   cx_tree t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 0
        GROUP BY cm.recipe_id
    ),
    resource_flow AS (
        SELECT
            rf.item_id,
            SUM(CASE rf.direction
                    WHEN 1 THEN  ar.total_mult * rf.qty_per_min
                    WHEN 0 THEN -ar.total_mult * rf.qty_per_min
                END) AS net_qty
        FROM   all_recipes   ar
        JOIN   resource_flows rf ON rf.parent_type = 0 AND rf.recipe_id = ar.recipe_id
        WHERE  rf.qty_per_min IS NOT NULL
        GROUP  BY rf.item_id
    )
    SELECT 1, p_complex_id, p_complex_id, item_id,
           CASE WHEN net_qty > 0 THEN 1 ELSE 0 END,
           ABS(net_qty)
    FROM   resource_flow
    WHERE  net_qty <> 0;

    -- Рабочие и электричество
    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id AS cid, 1.0::NUMERIC(12, 4) AS eff_mult
        UNION ALL
        SELECT cm.child_id, t.eff_mult * cm.multiplier
        FROM   cx_tree t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id, SUM(t.eff_mult * cm.multiplier) AS total_mult
        FROM   cx_tree t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 0
        GROUP BY cm.recipe_id
    )
    SELECT
        COALESCE(SUM(ar.total_mult * COALESCE(b.workers,        0)), 0),
        COALESCE(SUM(ar.total_mult * COALESCE(b.electricity_kw, 0)), 0)
    INTO v_workers, v_electricity
    FROM  all_recipes ar
    JOIN  recipes     r  ON r.id  = ar.recipe_id
    LEFT JOIN buildings b ON b.id = r.machine_id;

    UPDATE complexes
    SET    total_workers = v_workers, total_electricity_kw = v_electricity
    WHERE  id = p_complex_id;

    -- Обслуживание
    DELETE FROM complex_maintenance WHERE complex_id = p_complex_id;

    INSERT INTO complex_maintenance (complex_id, item, rate_per_min)
    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id AS cid, 1.0::NUMERIC(12, 4) AS eff_mult
        UNION ALL
        SELECT cm.child_id, t.eff_mult * cm.multiplier
        FROM   cx_tree t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id, SUM(t.eff_mult * cm.multiplier) AS total_mult
        FROM   cx_tree t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 0
        GROUP BY cm.recipe_id
    )
    SELECT p_complex_id, bm.item, SUM(ar.total_mult * bm.rate_per_min)
    FROM  all_recipes ar
    JOIN  recipes     r  ON r.id          = ar.recipe_id
    JOIN  building_maintenance bm ON bm.building_id = r.machine_id
    GROUP BY bm.item;
END;
$$;

COMMENT ON FUNCTION recalculate_complex(INTEGER) IS
'Пересчитывает resource_flows (parent_type=1), complex_maintenance, workers/electricity для комплекса.
 Вызывать вручную после изменений в complex_members.
 При изменении подкомплекса пересчитать и родительские:
   SELECT complex_id FROM complex_members WHERE child_type=1 AND child_id=<id>;';


COMMIT;
