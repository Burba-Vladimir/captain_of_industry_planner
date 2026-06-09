-- Migration v4: computing_tf for buildings, power_multiplier for recipes
-- Run once against the production DB before deploying v4 of the app.
--
-- Safe to run multiple times (IF NOT EXISTS / already-applied guards).

-- 1. Add computing_tf to buildings
--    Positive = generates computing (server rooms)
--    Negative = consumes computing (advanced machines)
ALTER TABLE buildings
    ADD COLUMN IF NOT EXISTS computing_tf NUMERIC NOT NULL DEFAULT 0;

-- 2. Add power_multiplier to recipes
--    Multiplied into electricity_kw at query time; default 1.0 = no change
ALTER TABLE recipes
    ADD COLUMN IF NOT EXISTS power_multiplier NUMERIC NOT NULL DEFAULT 1.0;

-- 3. Update recalculate_complex() to use power_multiplier when summing electricity
--    (replaces the previous version; safe to run on an already-updated schema)
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
           COALESCE(SUM(ar.total_mult * COALESCE(b.electricity_kw, 0) * COALESCE(r.power_multiplier, 1.0)), 0)
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

-- After running this migration, execute backfill_v4.py to populate the values
-- from the captain-of-data JSON.
