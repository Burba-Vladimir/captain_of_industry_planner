-- =============================================================
-- Captain of Industry — схема БД (PostgreSQL)
-- =============================================================

-- -------------------------------------------------------------
-- 1. Предметы (все входящие/исходящие объекты рецептов)
-- -------------------------------------------------------------
CREATE TABLE items (
    id   SERIAL      PRIMARY KEY,
    name VARCHAR(200) NOT NULL,

    CONSTRAINT uq_items_name UNIQUE (name)
);

COMMENT ON TABLE  items      IS 'Все предметы/ресурсы игры, участвующие в рецептах';
COMMENT ON COLUMN items.name IS 'Каноническое название предмета из вики';


-- -------------------------------------------------------------
-- 2. Здания
-- -------------------------------------------------------------
CREATE TABLE buildings (
    id             SERIAL       PRIMARY KEY,
    name           VARCHAR(200) NOT NULL,
    workers        SMALLINT     CHECK (workers >= 0),
    electricity_kw NUMERIC(10, 2),  -- > 0: потребление; < 0: выработка; NULL: не электрическое
    footprint      VARCHAR(20),
    designation    VARCHAR(100),

    CONSTRAINT uq_buildings_name UNIQUE (name)
);

COMMENT ON TABLE  buildings               IS 'Производственные здания и установки';
COMMENT ON COLUMN buildings.workers       IS 'Количество рабочих';
COMMENT ON COLUMN buildings.electricity_kw IS 'Электричество в кВт: > 0 потребление, < 0 выработка, NULL = не электрическое';
COMMENT ON COLUMN buildings.footprint     IS 'Занимаемая площадь, напр. "5x8"';
COMMENT ON COLUMN buildings.designation   IS 'Категория здания (Food Production, Smelting и т.д.)';

CREATE INDEX idx_buildings_designation ON buildings (designation);


-- -------------------------------------------------------------
-- 3. Требования к обслуживанию зданий
-- -------------------------------------------------------------
CREATE TABLE building_maintenance (
    id           SERIAL  PRIMARY KEY,
    building_id  INTEGER NOT NULL
                     REFERENCES buildings (id) ON DELETE CASCADE,
    item         VARCHAR(100) NOT NULL,   -- Maintenance I / II / III
    rate         NUMERIC(8, 4) NOT NULL CHECK (rate > 0),
    period_s     SMALLINT      NOT NULL CHECK (period_s > 0),
    rate_per_min NUMERIC(8, 4) NOT NULL CHECK (rate_per_min > 0),

    CONSTRAINT uq_building_maint UNIQUE (building_id, item)
);

COMMENT ON TABLE  building_maintenance             IS 'Расход запчастей для обслуживания здания';
COMMENT ON COLUMN building_maintenance.item        IS 'Тип запчастей: Maintenance I/II/III';
COMMENT ON COLUMN building_maintenance.rate        IS 'Количество запчастей за period_s секунд';
COMMENT ON COLUMN building_maintenance.period_s    IS 'Период расхода в секундах';
COMMENT ON COLUMN building_maintenance.rate_per_min IS 'Расход запчастей в минуту (rate / period_s * 60)';

CREATE INDEX idx_bld_maint_building ON building_maintenance (building_id);
CREATE INDEX idx_bld_maint_item     ON building_maintenance (item);


-- -------------------------------------------------------------
-- 4. Строительная стоимость зданий
-- -------------------------------------------------------------
CREATE TABLE building_construction (
    id          SERIAL  PRIMARY KEY,
    building_id INTEGER NOT NULL
                    REFERENCES buildings (id) ON DELETE CASCADE,
    item        VARCHAR(200) NOT NULL,
    qty         SMALLINT     NOT NULL CHECK (qty > 0),

    CONSTRAINT uq_building_constr UNIQUE (building_id, item)
);

COMMENT ON TABLE  building_construction      IS 'Материалы, необходимые для постройки здания';
COMMENT ON COLUMN building_construction.item IS 'Название строительного материала';
COMMENT ON COLUMN building_construction.qty  IS 'Количество единиц материала';

CREATE INDEX idx_bld_constr_building ON building_construction (building_id);


-- -------------------------------------------------------------
-- 5. Рецепты
-- -------------------------------------------------------------
CREATE TABLE recipes (
    id           SERIAL  PRIMARY KEY,
    machine_id   INTEGER REFERENCES buildings (id) ON DELETE SET NULL,
    machine_name VARCHAR(200) NOT NULL,
    cycle_time_s NUMERIC(8, 2) CHECK (cycle_time_s > 0),
    deprecated   BOOLEAN NOT NULL DEFAULT FALSE
);

COMMENT ON TABLE  recipes              IS 'Производственные рецепты';
COMMENT ON COLUMN recipes.machine_id   IS 'FK на здание (NULL если здания нет в таблице buildings)';
COMMENT ON COLUMN recipes.machine_name IS 'Денормализованное название машины — для удобства';
COMMENT ON COLUMN recipes.cycle_time_s IS 'Длительность одного цикла производства в секундах';
COMMENT ON COLUMN recipes.deprecated   IS 'TRUE = устаревший рецепт (машина более низкого уровня)';

CREATE INDEX idx_recipes_machine_id   ON recipes (machine_id);
CREATE INDEX idx_recipes_machine_name ON recipes (machine_name);
CREATE INDEX idx_recipes_cycle_time   ON recipes (cycle_time_s);


-- -------------------------------------------------------------
-- 6. Потоки ресурсов  (рецепты и комплексы — в одной таблице)
--
--    parent_type = 0 → рецепт   (recipe_id заполнен,  complex_id NULL)
--    parent_type = 1 → комплекс (complex_id заполнен, recipe_id  NULL)
--
--    parent_id  — денормализованный ID без FK, для удобства запросов;
--    recipe_id  — FK на recipes  (ссылочная целостность при parent_type=0);
--    complex_id — INTEGER без FK здесь; FK на complexes добавляется
--                 в complexes.sql после создания таблицы complexes.
--
--    direction: 0 = потребляется (input), 1 = производится (output)
--
--    qty_per_cycle — только для рецептов (NULL для комплексов);
--    qty_per_min   — нетто-поток; для комплексов всегда > 0,
--                    знак направления кодирует direction.
-- -------------------------------------------------------------
CREATE TABLE resource_flows (
    id            SERIAL   PRIMARY KEY,

    parent_type   SMALLINT NOT NULL CHECK (parent_type IN (0, 1)),
    parent_id     INTEGER  NOT NULL,
    recipe_id     INTEGER  REFERENCES recipes (id) ON DELETE CASCADE,
    complex_id    INTEGER,  -- FK добавляется в complexes.sql

    item_id       INTEGER  NOT NULL REFERENCES items (id),
    direction     SMALLINT NOT NULL CHECK (direction IN (0, 1)),

    qty_per_cycle SMALLINT       CHECK (qty_per_cycle > 0),  -- только для рецептов
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
COMMENT ON COLUMN resource_flows.complex_id    IS 'FK на complexes (добавлен в complexes.sql); заполнен только при parent_type = 1';
COMMENT ON COLUMN resource_flows.direction     IS '0 = потребляется (input), 1 = производится (output)';
COMMENT ON COLUMN resource_flows.qty_per_cycle IS 'Количество за цикл — только для рецептов (NULL у комплексов)';
COMMENT ON COLUMN resource_flows.qty_per_min   IS 'Количество в минуту; для комплексов — нетто-агрегат';
COMMENT ON COLUMN resource_flows.sort_order    IS 'Порядок отображения (как на вики); для комплексов = 0';

-- Основные паттерны доступа
CREATE INDEX idx_rf_parent        ON resource_flows (parent_type, parent_id);
CREATE INDEX idx_rf_recipe        ON resource_flows (recipe_id);
CREATE INDEX idx_rf_complex       ON resource_flows (complex_id);
CREATE INDEX idx_rf_item          ON resource_flows (item_id);
CREATE INDEX idx_rf_parent_dir    ON resource_flows (parent_type, parent_id, direction);
CREATE INDEX idx_rf_item_dir      ON resource_flows (item_id, direction);


-- =============================================================
-- Полезные представления
-- =============================================================

-- Рецепт целиком в одной строке (для быстрого просмотра)
CREATE VIEW v_recipes_full AS
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


-- Сводка по зданию: характеристики + обслуживание
CREATE VIEW v_buildings_full AS
SELECT
    b.id,
    b.name,
    b.workers,
    b.electricity_kw,
    b.footprint,
    b.designation,
    json_agg(
        json_build_object(
            'item',         bm.item,
            'rate_per_min', bm.rate_per_min
        )
    ) FILTER (WHERE bm.id IS NOT NULL)  AS maintenance,
    json_agg(
        json_build_object(
            'item', bc.item,
            'qty',  bc.qty
        )
    ) FILTER (WHERE bc.id IS NOT NULL)  AS construction
FROM buildings b
LEFT JOIN building_maintenance   bm ON bm.building_id = b.id
LEFT JOIN building_construction  bc ON bc.building_id = b.id
GROUP BY b.id;

COMMENT ON VIEW v_buildings_full IS 'Здания с расходами на обслуживание и стоимостью постройки';
