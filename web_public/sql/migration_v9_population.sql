-- Migration v9: страница «Нужды поселения».
--
-- Справочник потребностей населения плюс точечные правки игровых данных.
--
-- Нормы заданы на 10 000 жителей и масштабируются линейно.
-- Идемпотентно.

CREATE TABLE IF NOT EXISTS population_needs (
    id          SERIAL       PRIMARY KEY,
    -- Имя предмета как в items.name. NULL — для нагрузок без предмета (электроэнергия).
    item        TEXT,
    -- Подпись для интерфейса, если предмета в items нет.
    label       TEXT         NOT NULL,
    -- 0 = потребление (произвести и привезти)
    -- 1 = отход       (принять и переработать)
    -- 2 = нагрузка    (должна присутствовать в сети, не возится конвейером)
    need_type   SMALLINT     NOT NULL CHECK (need_type IN (0, 1, 2)),
    -- Норма на 10 000 жителей в единицах, указанных в unit.
    qty_per_10k NUMERIC(12, 4) NOT NULL CHECK (qty_per_10k > 0),
    unit        TEXT         NOT NULL CHECK (unit IN ('per_min', 'kw', 'tf')),
    -- Смысловая группа для группировки в интерфейсе.
    group_key   TEXT         NOT NULL,
    -- Взаимоисключающие варианты одной потребности (уровни лекарств).
    -- Строки с одинаковым choice_group — альтернативы: объём потребления у них
    -- одинаковый, отличается только эффект и производственная цепочка.
    choice_group TEXT,
    is_default   BOOLEAN     NOT NULL DEFAULT TRUE,
    -- Группа питания (1..4). Каждая группа закрывает 25% потребности в еде,
    -- продукты внутри группы делят её долю поровну. Нормы в qty_per_10k
    -- заданы для случая «поставляется всё»; при отключении продукта его доля
    -- переходит соседям по группе, при отключении всей группы — оставшимся
    -- группам поровну.
    food_group   SMALLINT,
    sort_order   SMALLINT    NOT NULL DEFAULT 0
);

ALTER TABLE population_needs ADD COLUMN IF NOT EXISTS food_group SMALLINT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_population_needs_label ON population_needs (label);
CREATE INDEX IF NOT EXISTS idx_population_needs_group ON population_needs (group_key, sort_order);

-- Данные из people.txt. Перезаливаем целиком, чтобы миграция была идемпотентной.
DELETE FROM population_needs;

INSERT INTO population_needs (item, label, need_type, qty_per_10k, unit, group_key, choice_group, is_default, food_group, sort_order) VALUES
    -- ── Еда ────────────────────────────────────────────────────────────
    -- «Полная» норма продукта (сколько его нужно, если он один закрывает всю
    -- четверть) = qty_per_10k, умноженная на число продуктов в группе.
    -- Отсюда: картофель 105, кукуруза 75, хлеб 50, мясо 67,5, яйца 75,
    -- тофу 45, сосиски 83,75, овощи 105, фрукты 78,75, снэки 65, пироги 62,5.
    ('Potato',                'Картофель',        0, 35,          'per_min', 'food',    NULL,       TRUE, 1,     10),
    ('Corn',                  'Кукуруза',         0, 25,          'per_min', 'food',    NULL,       TRUE, 1,     20),
    ('Bread',                 'Хлеб',             0, 16.6666667,  'per_min', 'food',    NULL,       TRUE, 1,     30),
    ('Meat',                  'Мясо',             0, 16.875,      'per_min', 'food',    NULL,       TRUE, 2,     40),
    ('Eggs',                  'Яйца',             0, 18.75,       'per_min', 'food',    NULL,       TRUE, 2,     50),
    ('Tofu',                  'Тофу',             0, 11.25,       'per_min', 'food',    NULL,       TRUE, 2,     60),
    ('Sausage',               'Сосиски',          0, 20.9375,     'per_min', 'food',    NULL,       TRUE, 2,     70),
    ('Vegetables',            'Овощи',            0, 52.5,        'per_min', 'food',    NULL,       TRUE, 3,     80),
    ('Fruit',                 'Фрукты',           0, 39.375,      'per_min', 'food',    NULL,       TRUE, 3,     90),
    ('Snack',                 'Снэки',            0, 32.5,        'per_min', 'food',    NULL,       TRUE, 4,    100),
    ('Cake',                  'Пироги',           0, 31.25,       'per_min', 'food',    NULL,       TRUE, 4,    110),

    -- ── Здоровье ───────────────────────────────────────────────────────
    -- Объём потребления одинаков для всех уровней; уровень влияет только на
    -- здоровье, единство и смертность при эпидемиях. Выбор — за пользователем.
    ('Medical Supplies',      'Лекарства I',      0, 50,          'per_min', 'health',  'medicine', FALSE, NULL, 200),
    ('Medical Supplies II',   'Лекарства II',     0, 50,          'per_min', 'health',  'medicine', FALSE, NULL, 210),
    ('Medical Supplies III',  'Лекарства III',    0, 50,          'per_min', 'health',  'medicine', TRUE, NULL, 220),

    -- ── Товары ─────────────────────────────────────────────────────────
    ('Household Goods',       'Хозтовары',        0, 110,         'per_min', 'goods',   NULL,       TRUE, NULL, 300),
    ('Household Appliances',  'Бытовая техника',  0, 77,          'per_min', 'goods',   NULL,       TRUE, NULL, 310),
    ('Consumer Electronics',  'Электроника',      0, 36,          'per_min', 'goods',   NULL,       TRUE, NULL, 320),
    ('Luxury Goods',          'Предметы роскоши', 0, 39.6,        'per_min', 'goods',   NULL,       TRUE, NULL, 330),

    -- ── Коммунальное ───────────────────────────────────────────────────
    ('Water',                 'Вода',             0, 576,         'per_min', 'utility', NULL,       TRUE, NULL, 400),
    ('Waste Water',           'Сточные воды',     1, 480,         'per_min', 'utility', NULL,       TRUE, NULL, 410),
    ('Waste',                 'Мусор',            1, 313.5,       'per_min', 'utility', NULL,       TRUE, NULL, 420),
    ('Electricity',           'Электроэнергия',   2, 15400,       'kw',      'utility', NULL,       TRUE, NULL, 430),
    ('Computing',             'Интернет',         2, 576,         'tf',      'utility', NULL,       TRUE, NULL, 440);

-- ─────────────────────────────────────────────────────────────────────────
-- Точечные правки игровых данных, найденные при разработке страницы
-- ─────────────────────────────────────────────────────────────────────────

-- 1) Русское название Mainframe Computer было испорчено (`"D!2!C`).
UPDATE content_translations ct
SET    value = 'ЭВМ'
FROM   buildings b
WHERE  b.po_key = ct.po_key
  AND  b.name   = 'Mainframe Computer'
  AND  ct.lang  = 'ru';

INSERT INTO content_translations (po_key, lang, value)
SELECT b.po_key, 'ru', 'ЭВМ'
FROM   buildings b
WHERE  b.name = 'Mainframe Computer'
  AND  b.po_key IS NOT NULL
  AND  NOT EXISTS (SELECT 1 FROM content_translations ct
                   WHERE ct.po_key = b.po_key AND ct.lang = 'ru');

-- 2) У Power Generator стояло -3000 кВт, тогда как рецепт выдаёт 2000/мин.
UPDATE buildings
SET    electricity_kw = -2000
WHERE  name = 'Power Generator'
  AND  electricity_kw = -3000;

-- 3) Дубль предмета «Water pollution» (со строчной p): тот же po_key, но
--    ни одного потока и ни одной ссылки. Удаляем только если это так и есть.
DELETE FROM items i
WHERE  i.name = 'Water pollution'
  AND  EXISTS (SELECT 1 FROM items o WHERE o.name = 'Water Pollution')
  AND  NOT EXISTS (SELECT 1 FROM resource_flows        rf  WHERE rf.item_id = i.id)
  AND  NOT EXISTS (SELECT 1 FROM building_maintenance  bm  WHERE bm.item = i.name)
  AND  NOT EXISTS (SELECT 1 FROM building_construction bc  WHERE bc.item = i.name)
  AND  NOT EXISTS (SELECT 1 FROM complex_maintenance   cm  WHERE cm.item = i.name)
  AND  NOT EXISTS (SELECT 1 FROM complex_construction  cc  WHERE cc.item = i.name)
  AND  NOT EXISTS (SELECT 1 FROM complex_edges         ce  WHERE ce.resource_item = i.name)
  AND  NOT EXISTS (SELECT 1 FROM complex_members       cmb WHERE cmb.idle_item = i.name)
  AND  NOT EXISTS (SELECT 1 FROM population_needs      pn  WHERE pn.item = i.name);

-- 4) Птицеферма: в базе были округлены и выход яиц, и оба входа.
--    Фактически 7,32 яйца, 15,1 корма и 18,1 воды в минуту.
UPDATE resource_flows rf
SET    qty_per_min   = v.val,
       qty_per_cycle = v.val * r.cycle_time_s / 60
FROM   recipes r, buildings b, items i,
       (VALUES ('Animal Feed', 15.1), ('Water', 18.1)) AS v(item, val)
WHERE  rf.parent_type = 0
  AND  rf.recipe_id   = r.id
  AND  b.id = r.machine_id AND b.name = 'Chicken Farm'
  AND  i.id = rf.item_id   AND i.name = v.item
  AND  rf.direction = 0
  AND  rf.qty_per_min <> v.val;

-- Выход яиц.
UPDATE resource_flows rf
SET    qty_per_min   = 7.32,
       qty_per_cycle = 7.32 * r.cycle_time_s / 60
FROM   recipes r, buildings b, items i
WHERE  rf.parent_type = 0
  AND  rf.recipe_id   = r.id
  AND  b.id = r.machine_id AND b.name = 'Chicken Farm'
  AND  i.id = rf.item_id   AND i.name = 'Eggs'
  AND  rf.direction = 1
  AND  rf.qty_per_min <> 7.32;

-- ─────────────────────────────────────────────────────────────────────────
-- Рецепты ферм по фактическим игровым значениям (источник: farms.txt)
-- ─────────────────────────────────────────────────────────────────────────
--
-- Данные из базы были неточными: вода округлена (у пшеницы 191 вместо 190,75),
-- а варианты «без удобрений» для орошаемой плантации и теплиц отсутствовали
-- вовсе. Здесь задаются все 115 сочетаний «ферма x удобрение x культура».
--
-- Урожай в минуту не хранится в источнике и считается: урожай за цикл x 60
-- делить на длительность цикла.
--
-- Рецепты Flowers в файле не заданы и остаются нетронутыми.
-- Идемпотентно: повторный прогон переписывает те же значения.

-- Урожай за цикл стал дробным (43,2 у рапса на удобрении 1), а колонка была
-- целочисленной. Менять тип мешает представление v_recipes_full — снимаем его
-- и возвращаем следом (v_complexes_full эту колонку не использует).
DROP VIEW IF EXISTS v_recipes_full;
ALTER TABLE resource_flows ALTER COLUMN qty_per_cycle TYPE NUMERIC(14, 6);
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

-- Без ON COMMIT DROP: psql выполняет каждый запрос в своей транзакции,
-- и таблица исчезла бы ещё до вставки. Удаляем её явно в конце.
DROP TABLE IF EXISTS farm_src;
CREATE TEMP TABLE farm_src (
    farm      TEXT    NOT NULL,
    crop      TEXT    NOT NULL,
    cycle_s   NUMERIC NOT NULL,
    yield_c   NUMERIC NOT NULL,
    water_c   NUMERIC NOT NULL,
    fert_c    NUMERIC NOT NULL,
    fert_item TEXT
);

INSERT INTO farm_src (farm, crop, cycle_s, yield_c, water_c, fert_c, fert_item) VALUES
    ('Irrigated Farm', 'Canola', 180, 36, 83.75, 27, 'Fertilizer (Organic)'),
    ('Irrigated Farm', 'Corn', 240, 66, 160, 48, 'Fertilizer (Organic)'),
    ('Irrigated Farm', 'Potato', 180, 58, 108, 31.5, 'Fertilizer (Organic)'),
    ('Irrigated Farm', 'Soybean', 240, 22, 144, 60, 'Fertilizer (Organic)'),
    ('Irrigated Farm', 'Tree Sapling', 720, 60, 324, 72, 'Fertilizer (Organic)'),
    ('Irrigated Farm', 'Vegetables', 240, 60, 128.4, 42, 'Fertilizer (Organic)'),
    ('Irrigated Farm', 'Wheat', 360, 58, 190.75, 63, 'Fertilizer (Organic)'),
    ('Irrigated Farm', 'Canola', 180, 43.2, 83.75, 20.7, 'Fertilizer I'),
    ('Irrigated Farm', 'Corn', 240, 79.2, 160, 36, 'Fertilizer I'),
    ('Irrigated Farm', 'Potato', 180, 69.6, 108, 23.85, 'Fertilizer I'),
    ('Irrigated Farm', 'Soybean', 240, 26.4, 144, 44.4, 'Fertilizer I'),
    ('Irrigated Farm', 'Tree Sapling', 720, 72, 324, 57.6, 'Fertilizer I'),
    ('Irrigated Farm', 'Vegetables', 240, 72, 128.4, 31.8, 'Fertilizer I'),
    ('Irrigated Farm', 'Wheat', 360, 69.6, 190.75, 47.7, 'Fertilizer I'),
    ('Irrigated Farm', 'Canola', 180, 50.4, 83.75, 22.32, 'Fertilizer II'),
    ('Irrigated Farm', 'Corn', 240, 92.4, 160, 38.4, 'Fertilizer II'),
    ('Irrigated Farm', 'Potato', 180, 81.2, 108, 25.56, 'Fertilizer II'),
    ('Irrigated Farm', 'Soybean', 240, 30.8, 144, 47.04, 'Fertilizer II'),
    ('Irrigated Farm', 'Tree Sapling', 720, 84, 324, 63.36, 'Fertilizer II'),
    ('Irrigated Farm', 'Vegetables', 240, 84, 128.4, 34.08, 'Fertilizer II'),
    ('Irrigated Farm', 'Wheat', 360, 81.2, 190.75, 51.12, 'Fertilizer II'),
    ('Greenhouse', 'Canola', 180, 45, 94.21875, 30.375, 'Fertilizer (Organic)'),
    ('Greenhouse', 'Corn', 240, 82.5, 180, 54, 'Fertilizer (Organic)'),
    ('Greenhouse', 'Potato', 180, 72.5, 121.5, 35.4375, 'Fertilizer (Organic)'),
    ('Greenhouse', 'Soybean', 240, 27.5, 162, 67.5, 'Fertilizer (Organic)'),
    ('Greenhouse', 'Tree Sapling', 720, 75, 364.5, 81, 'Fertilizer (Organic)'),
    ('Greenhouse', 'Vegetables', 240, 75, 144.45, 47.25, 'Fertilizer (Organic)'),
    ('Greenhouse', 'Wheat', 360, 72.5, 214.59375, 70.875, 'Fertilizer (Organic)'),
    ('Greenhouse', 'Canola', 180, 54, 94.21875, 23.0625, 'Fertilizer I'),
    ('Greenhouse', 'Corn', 240, 99, 180, 40.2, 'Fertilizer I'),
    ('Greenhouse', 'Potato', 180, 87, 121.5, 26.60625, 'Fertilizer I'),
    ('Greenhouse', 'Soybean', 240, 33, 162, 49.65, 'Fertilizer I'),
    ('Greenhouse', 'Tree Sapling', 720, 90, 364.5, 63.9, 'Fertilizer I'),
    ('Greenhouse', 'Vegetables', 240, 90, 144.45, 35.475, 'Fertilizer I'),
    ('Greenhouse', 'Wheat', 360, 87, 214.59375, 53.2125, 'Fertilizer I'),
    ('Greenhouse', 'Canola', 180, 63, 94.21875, 24.75, 'Fertilizer II'),
    ('Greenhouse', 'Corn', 240, 115.5, 180, 42.72, 'Fertilizer II'),
    ('Greenhouse', 'Potato', 180, 101.5, 121.5, 28.395, 'Fertilizer II'),
    ('Greenhouse', 'Soybean', 240, 38.5, 162, 52.44, 'Fertilizer II'),
    ('Greenhouse', 'Tree Sapling', 720, 105, 364.5, 69.84, 'Fertilizer II'),
    ('Greenhouse', 'Vegetables', 240, 105, 144.45, 37.86, 'Fertilizer II'),
    ('Greenhouse', 'Wheat', 360, 101.5, 214.59375, 56.79, 'Fertilizer II'),
    ('Greenhouse II', 'Canola', 180, 54, 104.6875, 33.75, 'Fertilizer (Organic)'),
    ('Greenhouse II', 'Corn', 240, 99, 200, 60, 'Fertilizer (Organic)'),
    ('Greenhouse II', 'Potato', 180, 87, 135, 39.375, 'Fertilizer (Organic)'),
    ('Greenhouse II', 'Soybean', 240, 33, 180, 75, 'Fertilizer (Organic)'),
    ('Greenhouse II', 'Tree Sapling', 720, 90, 405, 90, 'Fertilizer (Organic)'),
    ('Greenhouse II', 'Vegetables', 240, 90, 160.5, 52.5, 'Fertilizer (Organic)'),
    ('Greenhouse II', 'Wheat', 360, 87, 238.4375, 78.75, 'Fertilizer (Organic)'),
    ('Greenhouse II', 'Canola', 180, 64.8, 104.6875, 25.425, 'Fertilizer I'),
    ('Greenhouse II', 'Corn', 240, 118.8, 200, 44.4, 'Fertilizer I'),
    ('Greenhouse II', 'Potato', 180, 104.4, 135, 29.3625, 'Fertilizer I'),
    ('Greenhouse II', 'Soybean', 240, 39.6, 180, 54.9, 'Fertilizer I'),
    ('Greenhouse II', 'Tree Sapling', 720, 108, 405, 70.2, 'Fertilizer I'),
    ('Greenhouse II', 'Vegetables', 240, 108, 160.5, 39.15, 'Fertilizer I'),
    ('Greenhouse II', 'Wheat', 360, 104.4, 238.4375, 58.725, 'Fertilizer I'),
    ('Greenhouse II', 'Canola', 180, 75.6, 104.6875, 27.18, 'Fertilizer II'),
    ('Greenhouse II', 'Corn', 240, 138.6, 200, 47.04, 'Fertilizer II'),
    ('Greenhouse II', 'Potato', 180, 121.8, 135, 31.23, 'Fertilizer II'),
    ('Greenhouse II', 'Soybean', 240, 46.2, 180, 57.84, 'Fertilizer II'),
    ('Greenhouse II', 'Tree Sapling', 720, 126, 405, 76.32, 'Fertilizer II'),
    ('Greenhouse II', 'Vegetables', 240, 126, 160.5, 41.64, 'Fertilizer II'),
    ('Greenhouse II', 'Wheat', 360, 121.8, 238.4375, 62.46, 'Fertilizer II'),
    ('Greenhouse', 'Sugar Cane', 540, 247.5, 486, 151.875, 'Fertilizer (Organic)'),
    ('Greenhouse', 'Fruit', 480, 100, 358.875, 81, 'Fertilizer (Organic)'),
    ('Greenhouse', 'Poppy', 240, 31, 121.5, 40.5, 'Fertilizer (Organic)'),
    ('Greenhouse', 'Sugar Cane', 540, 297, 486, 111.7125, 'Fertilizer I'),
    ('Greenhouse', 'Fruit', 480, 120, 358.875, 61.5, 'Fertilizer I'),
    ('Greenhouse', 'Poppy', 240, 37.2, 121.5, 30.75, 'Fertilizer I'),
    ('Greenhouse', 'Sugar Cane', 540, 346.5, 486, 117.99, 'Fertilizer II'),
    ('Greenhouse', 'Fruit', 480, 140, 358.875, 66, 'Fertilizer II'),
    ('Greenhouse', 'Poppy', 240, 43.4, 121.5, 33, 'Fertilizer II'),
    ('Greenhouse II', 'Sugar Cane', 540, 297, 540, 168.75, 'Fertilizer (Organic)'),
    ('Greenhouse II', 'Fruit', 480, 120, 398.75, 90, 'Fertilizer (Organic)'),
    ('Greenhouse II', 'Poppy', 240, 37.2, 135, 45, 'Fertilizer (Organic)'),
    ('Greenhouse II', 'Sugar Cane', 540, 356.4, 540, 123.525, 'Fertilizer I'),
    ('Greenhouse II', 'Fruit', 480, 144, 398.75, 67.8, 'Fertilizer I'),
    ('Greenhouse II', 'Poppy', 240, 44.64, 135, 33.9, 'Fertilizer I'),
    ('Greenhouse II', 'Sugar Cane', 540, 415.8, 540, 130.14, 'Fertilizer II'),
    ('Greenhouse II', 'Fruit', 480, 168, 398.75, 72.48, 'Fertilizer II'),
    ('Greenhouse II', 'Poppy', 240, 52.08, 135, 36.24, 'Fertilizer II'),
    ('Irrigated Farm', 'Canola', 180, 25.2, 83.75, 0, NULL),
    ('Irrigated Farm', 'Corn', 240, 39.6, 160, 0, NULL),
    ('Irrigated Farm', 'Potato', 180, 37.7, 108, 0, NULL),
    ('Irrigated Farm', 'Soybean', 240, 11, 144, 0, NULL),
    ('Irrigated Farm', 'Tree Sapling', 720, 48, 324, 0, NULL),
    ('Irrigated Farm', 'Vegetables', 240, 39, 128.4, 0, NULL),
    ('Irrigated Farm', 'Wheat', 360, 37.7, 190.75, 0, NULL),
    ('Greenhouse', 'Canola', 180, 29.8125, 94.21875, 0, NULL),
    ('Greenhouse', 'Corn', 240, 45.375, 180, 0, NULL),
    ('Greenhouse', 'Potato', 180, 43.953125, 121.5, 0, NULL),
    ('Greenhouse', 'Soybean', 240, 12.03125, 162, 0, NULL),
    ('Greenhouse', 'Tree Sapling', 720, 58.125, 364.5, 0, NULL),
    ('Greenhouse', 'Vegetables', 240, 45.46875, 144.45, 0, NULL),
    ('Greenhouse', 'Wheat', 360, 43.953125, 214.59375, 0, NULL),
    ('Greenhouse II', 'Canola', 180, 33.75, 104.6875, 0, NULL),
    ('Greenhouse II', 'Corn', 240, 49.5, 200, 0, NULL),
    ('Greenhouse II', 'Potato', 180, 48.9375, 135, 0, NULL),
    ('Greenhouse II', 'Soybean', 240, 12.375, 180, 0, NULL),
    ('Greenhouse II', 'Tree Sapling', 720, 67.5, 405, 0, NULL),
    ('Greenhouse II', 'Vegetables', 240, 50.625, 160.5, 0, NULL),
    ('Greenhouse II', 'Wheat', 360, 48.9375, 238.4375, 0, NULL),
    ('Greenhouse', 'Sugar Cane', 540, 108.28125, 486, 0, NULL),
    ('Greenhouse', 'Fruit', 480, 66.25, 358.875, 0, NULL),
    ('Greenhouse', 'Poppy', 240, 20.5375, 121.5, 0, NULL),
    ('Greenhouse II', 'Sugar Cane', 540, 111.375, 540, 0, NULL),
    ('Greenhouse II', 'Fruit', 480, 75, 398.75, 0, NULL),
    ('Greenhouse II', 'Poppy', 240, 23.25, 135, 0, NULL),
    ('Farm', 'Canola', 180, 18.053731, 0, 0, NULL),
    ('Farm', 'Corn', 240, 19.8, 0, 0, NULL),
    ('Farm', 'Potato', 180, 20.944444, 0, 0, NULL),
    ('Farm', 'Soybean', 240, 6.111111, 0, 0, NULL),
    ('Farm', 'Tree Sapling', 720, 35.555556, 0, 0, NULL),
    ('Farm', 'Vegetables', 240, 24.299065, 0, 0, NULL),
    ('Farm', 'Wheat', 360, 23.716907, 0, 0, NULL);

DO $farm$
DECLARE
    s        RECORD;
    v_bld    INTEGER;
    v_crop   INTEGER;
    v_water  INTEGER;
    v_fert   INTEGER;
    v_recipe INTEGER;
    v_dep    BOOLEAN;
BEGIN
    SELECT id INTO v_water FROM items WHERE name = 'Water';

    FOR s IN SELECT * FROM farm_src LOOP
        SELECT id INTO v_bld  FROM buildings WHERE name = s.farm;
        SELECT id INTO v_crop FROM items     WHERE name = s.crop;
        IF v_bld IS NULL OR v_crop IS NULL THEN
            RAISE NOTICE 'пропущено: % / %', s.farm, s.crop;
            CONTINUE;
        END IF;
        v_fert := NULL;
        IF s.fert_item IS NOT NULL THEN
            SELECT id INTO v_fert FROM items WHERE name = s.fert_item;
        END IF;

        -- Рецепт опознаём по ферме, культуре и виду удобрения.
        SELECT r.id INTO v_recipe
        FROM recipes r
        JOIN resource_flows o ON o.parent_type = 0 AND o.recipe_id = r.id
                             AND o.direction = 1 AND o.item_id = v_crop
        WHERE r.machine_id = v_bld
          AND ( (v_fert IS NULL
                 AND NOT EXISTS (SELECT 1 FROM resource_flows f2
                                  JOIN items i2 ON i2.id = f2.item_id
                                  WHERE f2.parent_type = 0 AND f2.recipe_id = r.id
                                    AND f2.direction = 0 AND i2.name LIKE 'Fertilizer%'))
             OR (v_fert IS NOT NULL
                 AND EXISTS (SELECT 1 FROM resource_flows f2
                              WHERE f2.parent_type = 0 AND f2.recipe_id = r.id
                                AND f2.direction = 0 AND f2.item_id = v_fert)) )
        LIMIT 1;

        IF v_recipe IS NULL THEN
            -- Новый вариант «без удобрений». Флаг deprecated берём как у
            -- остальных рецептов этой же фермы: у теплицы первого уровня они
            -- помечены как вытесненные старшей.
            SELECT COALESCE(bool_or(deprecated), FALSE) INTO v_dep
            FROM recipes WHERE machine_id = v_bld;

            INSERT INTO recipes (machine_id, machine_name, cycle_time_s, deprecated)
            VALUES (v_bld, s.farm, s.cycle_s, v_dep)
            RETURNING id INTO v_recipe;

            INSERT INTO resource_flows
                (parent_type, parent_id, recipe_id, item_id, direction,
                 qty_per_cycle, qty_per_min, sort_order)
            VALUES (0, v_recipe, v_recipe, v_crop, 1,
                    s.yield_c, s.yield_c * 60 / s.cycle_s, 0);
        ELSE
            UPDATE recipes SET cycle_time_s = s.cycle_s WHERE id = v_recipe;
            UPDATE resource_flows
               SET qty_per_cycle = s.yield_c,
                   qty_per_min   = s.yield_c * 60 / s.cycle_s
             WHERE parent_type = 0 AND recipe_id = v_recipe
               AND direction = 1 AND item_id = v_crop;
        END IF;

        -- Вода
        IF s.water_c > 0 THEN
            INSERT INTO resource_flows
                (parent_type, parent_id, recipe_id, item_id, direction,
                 qty_per_cycle, qty_per_min, sort_order)
            VALUES (0, v_recipe, v_recipe, v_water, 0,
                    s.water_c, s.water_c * 60 / s.cycle_s, 0)
            ON CONFLICT (parent_type, parent_id, item_id, direction) DO UPDATE
                SET qty_per_cycle = EXCLUDED.qty_per_cycle,
                    qty_per_min   = EXCLUDED.qty_per_min;
        ELSE
            DELETE FROM resource_flows
             WHERE parent_type = 0 AND recipe_id = v_recipe
               AND direction = 0 AND item_id = v_water;
        END IF;

        -- Удобрение
        IF v_fert IS NOT NULL AND s.fert_c > 0 THEN
            INSERT INTO resource_flows
                (parent_type, parent_id, recipe_id, item_id, direction,
                 qty_per_cycle, qty_per_min, sort_order)
            VALUES (0, v_recipe, v_recipe, v_fert, 0,
                    s.fert_c, s.fert_c * 60 / s.cycle_s, 1)
            ON CONFLICT (parent_type, parent_id, item_id, direction) DO UPDATE
                SET qty_per_cycle = EXCLUDED.qty_per_cycle,
                    qty_per_min   = EXCLUDED.qty_per_min;
        END IF;
    END LOOP;
END
$farm$;

DROP TABLE IF EXISTS farm_src;
