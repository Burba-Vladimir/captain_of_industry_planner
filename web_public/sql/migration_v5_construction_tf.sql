-- Migration v5: Normalize total_computing_tf and construction for complexes
-- Adds:
--   complexes.total_computing_tf  — pre-computed by recalculate_complex (with efficiency)
--   complex_construction table    — aggregated build materials (same as complex_maintenance pattern)
--   Updates recalculate_complex() to populate both fields recursively
--
-- Safe to run multiple times (IF NOT EXISTS / CREATE OR REPLACE guards).
-- Run this migration, then the inline backfill at the bottom does the rest.

-- 1. Add total_computing_tf column
ALTER TABLE complexes
    ADD COLUMN IF NOT EXISTS total_computing_tf NUMERIC(12, 4);

-- 2. Create complex_construction table (mirrors complex_maintenance structure)
CREATE TABLE IF NOT EXISTS complex_construction (
    id         SERIAL  PRIMARY KEY,
    complex_id INTEGER NOT NULL REFERENCES complexes (id) ON DELETE CASCADE,
    item       VARCHAR(200) NOT NULL,
    qty        NUMERIC(12, 4) NOT NULL CHECK (qty > 0),
    CONSTRAINT uq_complex_construction UNIQUE (complex_id, item)
);

CREATE INDEX IF NOT EXISTS idx_cx_construction_complex ON complex_construction (complex_id);

-- 3. Replace recalculate_complex() — adds TF + construction blocks.
--    Workers / electricity / maintenance / flows unchanged (no efficiency in those CTEs
--    for backward-compat with existing stored values).
--    TF and construction use efficiency-aware CTE (matches previous inline computation).
CREATE OR REPLACE FUNCTION recalculate_complex(p_complex_id INTEGER)
RETURNS VOID LANGUAGE plpgsql AS $$
DECLARE
    v_workers      NUMERIC(12,2);
    v_electricity  NUMERIC(12,2);
    v_computing_tf NUMERIC(12,4);
BEGIN
    -- ── Resource flows ────────────────────────────────────────────────────────
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

    -- ── Workers + Electricity (physical multiplier, no efficiency — existing behaviour) ─
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

    -- ── Maintenance (physical multiplier, no efficiency — existing behaviour) ─────────
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

    -- ── Computing TF (effective: multiplier × efficiency at every level) ─────────────
    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id AS cid, 1.0::NUMERIC(12,4) AS eff_mult
        UNION ALL
        SELECT cm.child_id, (t.eff_mult * cm.multiplier * cm.efficiency)::NUMERIC(12,4)
        FROM cx_tree t JOIN complex_members cm ON cm.complex_id = t.cid WHERE cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id, SUM(t.eff_mult * cm.multiplier * cm.efficiency) AS total_mult
        FROM cx_tree t JOIN complex_members cm ON cm.complex_id = t.cid WHERE cm.child_type = 0
        GROUP BY cm.recipe_id
    )
    SELECT COALESCE(SUM(ar.total_mult * COALESCE(b.computing_tf, 0)), 0)
    INTO v_computing_tf
    FROM all_recipes ar JOIN recipes r ON r.id = ar.recipe_id LEFT JOIN buildings b ON b.id = r.machine_id;

    UPDATE complexes SET total_computing_tf = v_computing_tf WHERE id = p_complex_id;

    -- ── Construction (effective: multiplier × efficiency at every level) ─────────────
    DELETE FROM complex_construction WHERE complex_id = p_complex_id;
    INSERT INTO complex_construction (complex_id, item, qty)
    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id AS cid, 1.0::NUMERIC(12,4) AS eff_mult
        UNION ALL
        SELECT cm.child_id, (t.eff_mult * cm.multiplier * cm.efficiency)::NUMERIC(12,4)
        FROM cx_tree t JOIN complex_members cm ON cm.complex_id = t.cid WHERE cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id, SUM(t.eff_mult * cm.multiplier * cm.efficiency) AS total_mult
        FROM cx_tree t JOIN complex_members cm ON cm.complex_id = t.cid WHERE cm.child_type = 0
        GROUP BY cm.recipe_id
    )
    SELECT p_complex_id, bc.item, CEIL(SUM(ar.total_mult * bc.qty))
    FROM all_recipes ar
    JOIN recipes r  ON r.id  = ar.recipe_id
    JOIN building_construction bc ON bc.building_id = r.machine_id
    GROUP BY bc.item
    HAVING SUM(ar.total_mult * bc.qty) > 0;
END;
$$;

-- 4. Backfill all existing complexes (order by id is fine — function uses raw game data,
--    not stored totals of sub-complexes)
DO $$
DECLARE
    cid INTEGER;
    cnt INTEGER := 0;
BEGIN
    FOR cid IN SELECT id FROM complexes ORDER BY id LOOP
        PERFORM recalculate_complex(cid);
        cnt := cnt + 1;
    END LOOP;
    RAISE NOTICE 'migration_v5: backfilled % complexes', cnt;
END;
$$;

DO $$ BEGIN RAISE NOTICE 'Migration v5 complete: total_computing_tf + complex_construction added.'; END $$;
