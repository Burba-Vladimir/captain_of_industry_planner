-- =============================================================
-- Captain of Industry — модуль «Комплексы»
-- =============================================================
-- Зависимости: schema.sql должен быть выполнен раньше
--   (нужны таблицы: recipes, resource_flows, items,
--    buildings, building_maintenance)
-- =============================================================


-- -------------------------------------------------------------
-- 1. Комплексы
-- -------------------------------------------------------------
CREATE TABLE complexes (
    id                   SERIAL       PRIMARY KEY,
    name                 VARCHAR(200) NOT NULL,
    description          TEXT,

    -- Агрегаты — пересчитываются функцией recalculate_complex()
    total_workers        NUMERIC(12, 2),
    total_electricity_kw NUMERIC(12, 2),  -- > 0: потребление, < 0: выработка (нетто)

    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_complexes_name UNIQUE (name)
);

COMMENT ON TABLE  complexes                      IS 'Пользовательские комплексы — группы рецептов и подкомплексов';
COMMENT ON COLUMN complexes.total_workers        IS 'Суммарное число рабочих с учётом всех мультипликаторов';
COMMENT ON COLUMN complexes.total_electricity_kw IS 'Суммарное электричество (нетто): > 0 потребление, < 0 выработка';

CREATE INDEX idx_complexes_name ON complexes (name);


-- Теперь, когда complexes существует, добавляем FK на неё в resource_flows
ALTER TABLE resource_flows
    ADD CONSTRAINT fk_resource_flows_complex
    FOREIGN KEY (complex_id) REFERENCES complexes (id) ON DELETE CASCADE;


-- -------------------------------------------------------------
-- 2. Члены комплекса  (complex ↔ recipe|complex, many-to-many)
--
--    child_type = 0 → рецепт    (recipe_id заполнен, child_complex_id NULL)
--    child_type = 1 → подкомплекс (child_complex_id заполнен, recipe_id NULL)
--
--    child_id — денормализованный ID без FK, для удобства запросов.
--    Циклы запрещены триггером fn_check_complex_cycle.
-- -------------------------------------------------------------
CREATE TABLE complex_members (
    id               SERIAL   PRIMARY KEY,
    complex_id       INTEGER  NOT NULL REFERENCES complexes (id) ON DELETE CASCADE,

    child_type       SMALLINT NOT NULL CHECK (child_type IN (0, 1)),
    -- 0 = рецепт, 1 = подкомплекс

    child_id         INTEGER  NOT NULL,
    -- Денормализованный ID без FK: = recipe_id или = child_complex_id

    recipe_id        INTEGER  REFERENCES recipes  (id) ON DELETE CASCADE,
    child_complex_id INTEGER  REFERENCES complexes(id) ON DELETE CASCADE,

    multiplier       NUMERIC(10, 4) NOT NULL DEFAULT 1 CHECK (multiplier > 0),

    -- Ровно один FK должен быть заполнен, и совпадать с child_id
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

    -- Комплекс не может включать сам себя как подкомплекс
    CONSTRAINT no_self_reference CHECK (child_type = 0 OR complex_id <> child_id),

    CONSTRAINT uq_complex_member UNIQUE (complex_id, child_type, child_id)
);

COMMENT ON TABLE  complex_members                  IS 'Члены комплекса: рецепты (child_type=0) и подкомплексы (child_type=1)';
COMMENT ON COLUMN complex_members.child_type       IS '0 = рецепт, 1 = подкомплекс';
COMMENT ON COLUMN complex_members.child_id         IS 'Денормализованный ID без FK — для удобства запросов';
COMMENT ON COLUMN complex_members.recipe_id        IS 'FK на recipes; заполнен только при child_type = 0';
COMMENT ON COLUMN complex_members.child_complex_id IS 'FK на complexes; заполнен только при child_type = 1';
COMMENT ON COLUMN complex_members.multiplier       IS 'Сколько экземпляров рецепта/подкомплекса работает в составе комплекса';

CREATE INDEX idx_cx_members_complex        ON complex_members (complex_id);
CREATE INDEX idx_cx_members_recipe         ON complex_members (recipe_id);
CREATE INDEX idx_cx_members_child_complex  ON complex_members (child_complex_id);
CREATE INDEX idx_cx_members_type_child     ON complex_members (complex_id, child_type);


-- -------------------------------------------------------------
-- 3. Агрегированное обслуживание комплекса
--    (потоки ресурсов хранятся в общей таблице resource_flows
--     с parent_type = 1; обслуживание — отдельно, т.к. нет item_id)
-- -------------------------------------------------------------
CREATE TABLE complex_maintenance (
    id           SERIAL  PRIMARY KEY,
    complex_id   INTEGER NOT NULL REFERENCES complexes (id) ON DELETE CASCADE,
    item         VARCHAR(100) NOT NULL,           -- Maintenance I / II / III
    rate_per_min NUMERIC(12, 4) NOT NULL CHECK (rate_per_min > 0),

    CONSTRAINT uq_complex_maint UNIQUE (complex_id, item)
);

COMMENT ON TABLE  complex_maintenance              IS 'Суммарный расход запчастей на обслуживание всего комплекса';
COMMENT ON COLUMN complex_maintenance.item         IS 'Тип запчастей: Maintenance I / II / III';
COMMENT ON COLUMN complex_maintenance.rate_per_min IS 'Расход в минуту с учётом всех мультипликаторов';

CREATE INDEX idx_cx_maint_complex ON complex_maintenance (complex_id);


-- =============================================================
-- Триггерная функция: запрет циклов (только для подкомплексов)
-- =============================================================

CREATE OR REPLACE FUNCTION fn_check_complex_cycle()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    -- Для рецептов (child_type = 0) циклы невозможны — пропускаем
    IF NEW.child_type <> 1 THEN
        RETURN NEW;
    END IF;

    -- Проверяем: есть ли путь вниз от child_complex_id до complex_id?
    -- Если да — добавление ребра complex_id → child создаст цикл.
    IF EXISTS (
        WITH RECURSIVE descendants AS (
            SELECT NEW.child_id AS cid
            UNION ALL
            SELECT cm.child_id
            FROM   descendants      d
            JOIN   complex_members  cm ON cm.complex_id = d.cid
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

CREATE TRIGGER trg_no_complex_cycle
BEFORE INSERT OR UPDATE ON complex_members
FOR EACH ROW EXECUTE FUNCTION fn_check_complex_cycle();


-- =============================================================
-- Триггерная функция: автообновление updated_at
-- =============================================================

CREATE OR REPLACE FUNCTION fn_set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_complexes_updated_at
BEFORE UPDATE ON complexes
FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();


-- =============================================================
-- Функция пересчёта агрегатов комплекса
-- =============================================================
-- Вызывать вручную после любого изменения в complex_members.
-- При изменении подкомплекса пересчитайте также родительские:
--   SELECT complex_id FROM complex_members
--   WHERE child_type = 1 AND child_id = <изменённый_id>;
-- =============================================================

CREATE OR REPLACE FUNCTION recalculate_complex(p_complex_id INTEGER)
RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE
    v_workers     NUMERIC(12, 2);
    v_electricity NUMERIC(12, 2);
BEGIN

    -- ── Шаг 1: Нетто-ресурсы ────────────────────────────────
    --   Раскрываем дерево подкомплексов рекурсивным CTE,
    --   собираем все рецепты с накопленными мультипликаторами,
    --   считаем нетто из resource_flows (parent_type=0),
    --   записываем обратно в resource_flows (parent_type=1).

    DELETE FROM resource_flows
    WHERE  parent_type = 1
      AND  parent_id   = p_complex_id;

    INSERT INTO resource_flows
        (parent_type, parent_id, complex_id, item_id, direction, qty_per_min)
    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id        AS cid,
               1.0::NUMERIC(12, 4) AS eff_mult
        UNION ALL
        SELECT cm.child_id,
               t.eff_mult * cm.multiplier
        FROM   cx_tree         t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id,
               SUM(t.eff_mult * cm.multiplier) AS total_mult
        FROM   cx_tree         t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 0
        GROUP BY cm.recipe_id
    ),
    resource_flow AS (
        -- Нетто-поток: outputs со знаком "+", inputs со знаком "−"
        SELECT
            rf.item_id,
            SUM(
                CASE rf.direction
                    WHEN 1 THEN  ar.total_mult * rf.qty_per_min
                    WHEN 0 THEN -ar.total_mult * rf.qty_per_min
                END
            ) AS net_qty
        FROM   all_recipes   ar
        JOIN   resource_flows rf ON rf.parent_type = 0
                                AND rf.recipe_id   = ar.recipe_id
        WHERE  rf.qty_per_min IS NOT NULL
        GROUP  BY rf.item_id
    )
    SELECT
        1,                                           -- parent_type = комплекс
        p_complex_id,                                -- parent_id
        p_complex_id,                                -- complex_id (FK)
        item_id,
        CASE WHEN net_qty > 0 THEN 1 ELSE 0 END,    -- direction
        ABS(net_qty)                                 -- qty_per_min > 0
    FROM   resource_flow
    WHERE  net_qty <> 0;


    -- ── Шаг 2: Рабочие и электричество ──────────────────────

    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id        AS cid,
               1.0::NUMERIC(12, 4) AS eff_mult
        UNION ALL
        SELECT cm.child_id, t.eff_mult * cm.multiplier
        FROM   cx_tree         t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id,
               SUM(t.eff_mult * cm.multiplier) AS total_mult
        FROM   cx_tree         t
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
    SET    total_workers        = v_workers,
           total_electricity_kw = v_electricity
           -- updated_at проставит триггер trg_complexes_updated_at
    WHERE  id = p_complex_id;


    -- ── Шаг 3: Обслуживание ──────────────────────────────────

    DELETE FROM complex_maintenance WHERE complex_id = p_complex_id;

    INSERT INTO complex_maintenance (complex_id, item, rate_per_min)
    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id        AS cid,
               1.0::NUMERIC(12, 4) AS eff_mult
        UNION ALL
        SELECT cm.child_id, t.eff_mult * cm.multiplier
        FROM   cx_tree         t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id,
               SUM(t.eff_mult * cm.multiplier) AS total_mult
        FROM   cx_tree         t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 0
        GROUP BY cm.recipe_id
    )
    SELECT
        p_complex_id,
        bm.item,
        SUM(ar.total_mult * bm.rate_per_min) AS rate_per_min
    FROM  all_recipes          ar
    JOIN  recipes              r  ON r.id          = ar.recipe_id
    JOIN  building_maintenance bm ON bm.building_id = r.machine_id
    GROUP BY bm.item;

END;
$$;

COMMENT ON FUNCTION recalculate_complex(INTEGER) IS
'Полностью пересчитывает агрегаты комплекса:
   - resource_flows (parent_type=1) — нетто-ресурсы
   - complex_maintenance            — расход запчастей
   - complexes.total_workers / total_electricity_kw
 Раскрывает дерево подкомплексов через рекурсивный CTE.
 Источник данных для рецептов — resource_flows (parent_type=0).
 Вызывайте вручную после изменений в complex_members.
 При изменении подкомплекса пересчитайте и родительские комплексы:
   SELECT complex_id FROM complex_members
   WHERE child_type = 1 AND child_id = <изменённый_id>;';


-- =============================================================
-- Представление: комплекс с полной разбивкой
-- =============================================================

CREATE VIEW v_complexes_full AS
SELECT
    c.id,
    c.name,
    c.description,
    c.total_workers,
    c.total_electricity_kw,
    c.updated_at,

    -- Обслуживание
    (SELECT json_agg(
                json_build_object('item', cm.item,
                                  'rate_per_min', cm.rate_per_min)
                ORDER BY cm.item
            )
     FROM   complex_maintenance cm
     WHERE  cm.complex_id = c.id
    ) AS maintenance,

    -- Входящие ресурсы (direction = 0)
    (SELECT json_agg(
                json_build_object('item', i.name,
                                  'qty_per_min', rf.qty_per_min)
                ORDER BY i.name
            )
     FROM   resource_flows rf
     JOIN   items i ON i.id = rf.item_id
     WHERE  rf.parent_type = 1
       AND  rf.complex_id  = c.id
       AND  rf.direction   = 0
    ) AS inputs,

    -- Исходящие ресурсы (direction = 1)
    (SELECT json_agg(
                json_build_object('item', i.name,
                                  'qty_per_min', rf.qty_per_min)
                ORDER BY i.name
            )
     FROM   resource_flows rf
     JOIN   items i ON i.id = rf.item_id
     WHERE  rf.parent_type = 1
       AND  rf.complex_id  = c.id
       AND  rf.direction   = 1
    ) AS outputs

FROM complexes c;

COMMENT ON VIEW v_complexes_full IS
'Комплекс с агрегированными данными из resource_flows (parent_type=1): inputs/outputs, обслуживание, рабочие, электричество';


-- =============================================================
-- Представление: единый список рецептов и комплексов
-- =============================================================
-- Соглашение:
--   рецепт  → name = NULL, machine_name = строка
--   комплекс → name = строка, machine_name = NULL
--
-- electricity_kw: > 0 потребление, < 0 выработка, NULL — нет
-- =============================================================

CREATE VIEW v_nodes_full AS
SELECT * FROM (

SELECT
    'recipe'        AS node_type,
    r.id            AS node_id,
    NULL            AS name,
    r.machine_name,
    r.cycle_time_s,
    b.workers,
    b.electricity_kw,
    r.deprecated,

    (SELECT json_agg(json_build_object(
                'item',          i.name,
                'qty_per_cycle', rf.qty_per_cycle,
                'qty_per_min',   rf.qty_per_min)
                ORDER BY rf.sort_order)
     FROM resource_flows rf JOIN items i ON i.id = rf.item_id
     WHERE rf.parent_type = 0 AND rf.recipe_id = r.id AND rf.direction = 0
    ) AS inputs,

    (SELECT json_agg(json_build_object(
                'item',          i.name,
                'qty_per_cycle', rf.qty_per_cycle,
                'qty_per_min',   rf.qty_per_min)
                ORDER BY rf.sort_order)
     FROM resource_flows rf JOIN items i ON i.id = rf.item_id
     WHERE rf.parent_type = 0 AND rf.recipe_id = r.id AND rf.direction = 1
    ) AS outputs

FROM recipes  r
LEFT JOIN buildings b ON b.id = r.machine_id

UNION ALL

SELECT
    'complex'               AS node_type,
    c.id                    AS node_id,
    c.name                  AS name,
    NULL                    AS machine_name,
    NULL                    AS cycle_time_s,
    c.total_workers         AS workers,
    c.total_electricity_kw  AS electricity_kw,
    FALSE                   AS deprecated,

    (SELECT json_agg(json_build_object('item', i.name, 'qty_per_min', rf.qty_per_min)
                ORDER BY i.name)
     FROM resource_flows rf JOIN items i ON i.id = rf.item_id
     WHERE rf.parent_type = 1 AND rf.complex_id = c.id AND rf.direction = 0
    ) AS inputs,

    (SELECT json_agg(json_build_object('item', i.name, 'qty_per_min', rf.qty_per_min)
                ORDER BY i.name)
     FROM resource_flows rf JOIN items i ON i.id = rf.item_id
     WHERE rf.parent_type = 1 AND rf.complex_id = c.id AND rf.direction = 1
    ) AS outputs

FROM complexes c

) _nodes
ORDER BY COALESCE(machine_name, name);

COMMENT ON VIEW v_nodes_full IS
'Единый список рецептов и комплексов. Рецепт: name=NULL, machine_name=строка. Комплекс: name=строка, machine_name=NULL.';
