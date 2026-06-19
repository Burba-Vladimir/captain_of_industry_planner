-- Migration v7: Add Mainframe Computer building and recipe
-- Source: https://wiki.coigame.com/Mainframe_Computer
-- Workers: 12, Electricity: 2 MW (2000 kW), Computing: +8 TF
-- Construction: Construction Parts IV x100, Electronics II x200
-- Maintenance: Maintenance II, 14.0/min (14 items per 60s)
-- No resource inputs/outputs -- building simply provides computing TF
-- Idempotent: skips if building already exists.

DO $$
DECLARE
    v_building_id INTEGER;
    v_recipe_id   INTEGER;
BEGIN
    -- Skip if already applied
    IF EXISTS (SELECT 1 FROM buildings WHERE name = 'Mainframe Computer') THEN
        RAISE NOTICE 'Migration v7: Mainframe Computer already exists, skipping.';
        RETURN;
    END IF;

    -- 1. Insert building
    INSERT INTO buildings (name, workers, electricity_kw, computing_tf, designation, po_key)
    VALUES ('Mainframe Computer', 12, 2000, 8, 'Production', '__hardcoded__Mainframe Computer')
    RETURNING id INTO v_building_id;

    RAISE NOTICE 'Inserted building id=%', v_building_id;

    -- 2. Construction cost
    INSERT INTO building_construction (building_id, item, qty) VALUES
        (v_building_id, 'Construction Parts IV', 100),
        (v_building_id, 'Electronics II',        200);

    -- 3. Maintenance (rate=items per period_s, rate_per_min=rate*60/period_s)
    INSERT INTO building_maintenance (building_id, item, rate, period_s, rate_per_min)
    VALUES (v_building_id, 'Maintenance II', 14.0, 60, 14.0);

    -- 4. Recipe
    INSERT INTO recipes (machine_id, machine_name, cycle_time_s, deprecated, power_multiplier, wiki_id)
    VALUES (v_building_id, 'Mainframe Computer', 60, FALSE, 1.0, 'MainframeComputer')
    RETURNING id INTO v_recipe_id;

    RAISE NOTICE 'Inserted recipe id=%', v_recipe_id;

    -- 5a. Output: Computing x8 (8 per cycle, 8/min at 60s cycle)
    INSERT INTO resource_flows (parent_type, parent_id, recipe_id, item_id, direction, qty_per_cycle, qty_per_min, sort_order)
    VALUES (0, v_recipe_id, v_recipe_id, (SELECT id FROM items WHERE name = 'Computing'), 1, 8, 8.0, 0);

    -- 6. Translations
    -- EN
    INSERT INTO content_translations (po_key, lang, value)
    VALUES ('__hardcoded__Mainframe Computer', 'en', 'Mainframe Computer')
    ON CONFLICT (po_key, lang) DO NOTHING;
    -- RU: U+042D U+0412 U+041C = ЭВМ (Unicode escapes to avoid encoding issues)
    INSERT INTO content_translations (po_key, lang, value)
    VALUES ('__hardcoded__Mainframe Computer', 'ru', U&'\042D\0412\041C')
    ON CONFLICT (po_key, lang) DO UPDATE SET value = EXCLUDED.value;

END $$;

DO $$ BEGIN RAISE NOTICE 'Migration v7 complete.'; END $$;
