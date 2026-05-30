-- =============================================================
-- Captain of Industry — вставка данных
-- Порядок выполнения: секции 1 → 2 → 3 (соблюдать из-за FK)
-- =============================================================


-- =============================================================
-- СЕКЦИЯ 1: Предметы
-- Заменить содержимое между $json$ ... $json$ на items.json
-- =============================================================

INSERT INTO items (name)
SELECT value
FROM json_array_elements_text($json$

["ВСТАВИТЬ СЮДА СОДЕРЖИМОЕ items.json"]

$json$)
ON CONFLICT (name) DO NOTHING;


-- =============================================================
-- СЕКЦИЯ 2: Здания, обслуживание, строительная стоимость
-- Заменить содержимое между $json$ ... $json$ на buildings.json
-- =============================================================

DO $do$
DECLARE
    data   json := $json$

[{"name": "ВСТАВИТЬ СЮДА СОДЕРЖИМОЕ buildings.json"}]

    $json$;
    b      json;
    m      json;
    c      json;
    bld_id integer;
BEGIN
    FOR b IN SELECT * FROM json_array_elements(data) LOOP

        -- Здание (upsert: обновляем если уже есть, всегда получаем id)
        INSERT INTO buildings (name, workers, electricity_kw, footprint, designation)
        VALUES (
            b->>'name',
            (b->>'workers')::smallint,
            (b->>'electricity_kw')::numeric,
            b->>'footprint',
            b->>'designation'
        )
        ON CONFLICT (name) DO UPDATE SET
            workers        = EXCLUDED.workers,
            electricity_kw = EXCLUDED.electricity_kw,
            footprint      = EXCLUDED.footprint,
            designation    = EXCLUDED.designation
        RETURNING id INTO bld_id;

        -- Обслуживание
        IF b->'maintenance' IS NOT NULL AND
           json_array_length(b->'maintenance') > 0 THEN
            FOR m IN SELECT * FROM json_array_elements(b->'maintenance') LOOP
                INSERT INTO building_maintenance
                    (building_id, item, rate, period_s, rate_per_min)
                VALUES (
                    bld_id,
                    m->>'item',
                    (m->>'rate')::numeric,
                    (m->>'period_s')::smallint,
                    (m->>'rate_per_min')::numeric
                )
                ON CONFLICT (building_id, item) DO NOTHING;
            END LOOP;
        END IF;

        -- Строительная стоимость
        IF b->'construction' IS NOT NULL AND
           json_array_length(b->'construction') > 0 THEN
            FOR c IN SELECT * FROM json_array_elements(b->'construction') LOOP
                INSERT INTO building_construction (building_id, item, qty)
                VALUES (
                    bld_id,
                    c->>'item',
                    (c->>'qty')::smallint
                )
                ON CONFLICT (building_id, item) DO NOTHING;
            END LOOP;
        END IF;

    END LOOP;
END;
$do$;


-- =============================================================
-- СЕКЦИЯ 3: Рецепты и их ресурсы (→ resource_flows, parent_type=0)
-- Заменить содержимое между $json$ ... $json$ на recipes.json
-- =============================================================

DO $do$
DECLARE
    data       json := $json$

[{"machine": "ВСТАВИТЬ СЮДА СОДЕРЖИМОЕ recipes.json"}]

    $json$;
    r          json;
    slot       json;
    rec_id     integer;
    v_item_id  integer;
    ord        smallint;
BEGIN
    FOR r IN SELECT * FROM json_array_elements(data) LOOP

        -- Рецепт
        INSERT INTO recipes (machine_id, machine_name, cycle_time_s)
        VALUES (
            (SELECT id FROM buildings WHERE name = r->>'machine'),
            r->>'machine',
            (r->>'cycle_time_s')::numeric
        )
        RETURNING id INTO rec_id;

        -- Входящие ресурсы (direction = 0)
        ord := 0;
        FOR slot IN SELECT * FROM json_array_elements(r->'inputs') LOOP
            SELECT id INTO v_item_id FROM items WHERE name = slot->>'item';
            IF v_item_id IS NOT NULL THEN
                INSERT INTO resource_flows
                    (parent_type, parent_id, recipe_id,
                     item_id, direction, qty_per_cycle, qty_per_min, sort_order)
                VALUES (
                    0,
                    rec_id,
                    rec_id,
                    v_item_id,
                    0,
                    (slot->>'qty_per_cycle')::smallint,
                    (slot->>'qty_per_min')::numeric,
                    ord
                )
                ON CONFLICT (parent_type, parent_id, item_id, direction) DO NOTHING;
                ord := ord + 1;
            END IF;
        END LOOP;

        -- Исходящие ресурсы (direction = 1)
        ord := 0;
        FOR slot IN SELECT * FROM json_array_elements(r->'outputs') LOOP
            SELECT id INTO v_item_id FROM items WHERE name = slot->>'item';
            IF v_item_id IS NOT NULL THEN
                INSERT INTO resource_flows
                    (parent_type, parent_id, recipe_id,
                     item_id, direction, qty_per_cycle, qty_per_min, sort_order)
                VALUES (
                    0,
                    rec_id,
                    rec_id,
                    v_item_id,
                    1,
                    (slot->>'qty_per_cycle')::smallint,
                    (slot->>'qty_per_min')::numeric,
                    ord
                )
                ON CONFLICT (parent_type, parent_id, item_id, direction) DO NOTHING;
                ord := ord + 1;
            END IF;
        END LOOP;

    END LOOP;
END;
$do$;
