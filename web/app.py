"""
Captain of Industry — веб-интерфейс
────────────────────────────────────
Установка:   pip install flask psycopg2-binary
Запуск:      python app.py
Браузер:     http://localhost:5000
"""
from __future__ import annotations
import contextlib
import json

from flask import Flask, jsonify, render_template, request
import psycopg2
import psycopg2.extras

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────
# Настройки подключения к базе
# ─────────────────────────────────────────────────────────────────
DB = {
    "host":     "127.0.0.1",
    "port":     5432,
    "dbname":   "capitan_of_industry",
    "user":     "postgres",
    "password": "postgres",
}


@contextlib.contextmanager
def get_db():
    con = psycopg2.connect(**DB)
    try:
        yield con
    finally:
        con.close()


def _ensure_schema():
    """Идемпотентные миграции: добавляет столбцы/исправляет данные."""
    with get_db() as con:
        with con.cursor() as cur:
            # deprecated для комплексов (мог отсутствовать)
            cur.execute("""
                ALTER TABLE complexes
                    ADD COLUMN IF NOT EXISTS deprecated BOOLEAN NOT NULL DEFAULT FALSE;
            """)
            # NULL → FALSE для рецептов (на случай старых данных)
            cur.execute("""
                UPDATE recipes SET deprecated = FALSE WHERE deprecated IS NULL;
            """)
            # Визуальные координаты узлов в редакторе комплексов
            cur.execute("""
                ALTER TABLE complex_members
                    ADD COLUMN IF NOT EXISTS pos_x INT NOT NULL DEFAULT 0;
            """)
            cur.execute("""
                ALTER TABLE complex_members
                    ADD COLUMN IF NOT EXISTS pos_y INT NOT NULL DEFAULT 0;
            """)
            # Рёбра графа — визуальные соединения между узлами
            cur.execute("""
                CREATE TABLE IF NOT EXISTS complex_edges (
                    id             SERIAL  PRIMARY KEY,
                    complex_id     INT     NOT NULL
                                   REFERENCES complexes(id)       ON DELETE CASCADE,
                    from_member_id INT     NOT NULL
                                   REFERENCES complex_members(id)  ON DELETE CASCADE,
                    to_member_id   INT     NOT NULL
                                   REFERENCES complex_members(id)  ON DELETE CASCADE,
                    resource_item  TEXT    NOT NULL,
                    lcm_mode       BOOLEAN NOT NULL DEFAULT FALSE
                );
            """)
            # КПД узла (для режима «Простой»): 0..1, 1 = полная нагрузка
            cur.execute("""
                ALTER TABLE complex_members
                    ADD COLUMN IF NOT EXISTS efficiency NUMERIC(6,4) NOT NULL DEFAULT 1.0;
            """)
            # Ресурс, задающий простой (item + direction)
            cur.execute("""
                ALTER TABLE complex_members
                    ADD COLUMN IF NOT EXISTS idle_item VARCHAR(200) DEFAULT NULL;
            """)
            cur.execute("""
                ALTER TABLE complex_members
                    ADD COLUMN IF NOT EXISTS idle_direction VARCHAR(10) DEFAULT NULL;
            """)
            cur.execute("""
                ALTER TABLE complex_members
                    ADD COLUMN IF NOT EXISTS is_manual_partial BOOLEAN NOT NULL DEFAULT FALSE;
            """)
            cur.execute("""
                ALTER TABLE complex_members
                    ADD COLUMN IF NOT EXISTS external_ports TEXT;
            """)
            # Исправление recalculate_complex: PostgreSQL 14+ требует явного каста
            # в рекурсивной части CTE (NUMERIC(12,4) × NUMERIC(10,4) → NUMERIC, не NUMERIC(12,4))
            cur.execute("""
CREATE OR REPLACE FUNCTION recalculate_complex(p_complex_id INTEGER)
RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE
    v_workers     NUMERIC(12, 2);
    v_electricity NUMERIC(12, 2);
BEGIN
    DELETE FROM resource_flows
    WHERE  parent_type = 1 AND parent_id = p_complex_id;

    INSERT INTO resource_flows
        (parent_type, parent_id, complex_id, item_id, direction, qty_per_min)
    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id AS cid,
               1.0::NUMERIC(12,4) AS eff_mult
        UNION ALL
        SELECT cm.child_id,
               (t.eff_mult * cm.multiplier * COALESCE(cm.efficiency,1))::NUMERIC(12,4)
        FROM   cx_tree         t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id,
               SUM(t.eff_mult * cm.multiplier * COALESCE(cm.efficiency,1)) AS total_mult
        FROM   cx_tree         t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 0
        GROUP BY cm.recipe_id
    ),
    resource_flow AS (
        SELECT rf.item_id,
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

    -- phys_mult накапливает только multiplier (без efficiency) — для работников,
    -- eff_mult  накапливает multiplier*efficiency              — для электричества и ресурсов.
    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id AS cid,
               1.0::NUMERIC(12,4) AS eff_mult,
               1.0::NUMERIC(12,4) AS phys_mult
        UNION ALL
        SELECT cm.child_id,
               (t.eff_mult  * cm.multiplier * COALESCE(cm.efficiency,1))::NUMERIC(12,4),
               (t.phys_mult * cm.multiplier)::NUMERIC(12,4)
        FROM   cx_tree         t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id,
               SUM(t.eff_mult  * cm.multiplier * COALESCE(cm.efficiency,1)) AS total_mult,
               SUM(t.phys_mult * cm.multiplier)                             AS worker_mult
        FROM   cx_tree         t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 0
        GROUP BY cm.recipe_id
    )
    SELECT
        COALESCE(SUM(ar.worker_mult * COALESCE(b.workers,        0)), 0),
        COALESCE(SUM(ar.total_mult  * COALESCE(b.electricity_kw, 0)), 0)
    INTO v_workers, v_electricity
    FROM  all_recipes ar
    JOIN  recipes     r  ON r.id  = ar.recipe_id
    LEFT JOIN buildings b ON b.id = r.machine_id;

    UPDATE complexes
    SET    total_workers        = v_workers,
           total_electricity_kw = v_electricity
    WHERE  id = p_complex_id;

    DELETE FROM complex_maintenance WHERE complex_id = p_complex_id;

    INSERT INTO complex_maintenance (complex_id, item, rate_per_min)
    WITH RECURSIVE cx_tree AS (
        SELECT p_complex_id AS cid,
               1.0::NUMERIC(12,4) AS eff_mult
        UNION ALL
        SELECT cm.child_id,
               (t.eff_mult * cm.multiplier * COALESCE(cm.efficiency,1))::NUMERIC(12,4)
        FROM   cx_tree         t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 1
    ),
    all_recipes AS (
        SELECT cm.recipe_id,
               SUM(t.eff_mult * cm.multiplier * COALESCE(cm.efficiency,1)) AS total_mult
        FROM   cx_tree         t
        JOIN   complex_members cm ON cm.complex_id = t.cid
        WHERE  cm.child_type = 0
        GROUP BY cm.recipe_id
    )
    SELECT p_complex_id, bm.item,
           SUM(ar.total_mult * bm.rate_per_min)
    FROM  all_recipes          ar
    JOIN  recipes              r  ON r.id          = ar.recipe_id
    JOIN  building_maintenance bm ON bm.building_id = r.machine_id
    GROUP BY bm.item;
END;
$$;
            """)
        con.commit()


# ─────────────────────────────────────────────────────────────────
# Объединённый запрос: рецепты + комплексы
# ─────────────────────────────────────────────────────────────────
NODES_SQL = """\
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
        inp.items       AS inputs,
        out.items       AS outputs,
        mnt.items       AS maintenance

    FROM recipes r
    LEFT JOIN buildings b ON b.id = r.machine_id

    LEFT JOIN LATERAL (
        SELECT json_agg(
                   json_build_object(
                       'item',          i.name,
                       'qty_per_cycle', rf.qty_per_cycle,
                       'qty_per_min',   rf.qty_per_min
                   ) ORDER BY rf.sort_order
               ) AS items
        FROM  resource_flows rf
        JOIN  items i ON i.id = rf.item_id
        WHERE rf.parent_type = 0
          AND rf.recipe_id   = r.id
          AND rf.direction   = 0
    ) inp ON TRUE

    LEFT JOIN LATERAL (
        SELECT json_agg(
                   json_build_object(
                       'item',          i.name,
                       'qty_per_cycle', rf.qty_per_cycle,
                       'qty_per_min',   rf.qty_per_min
                   ) ORDER BY rf.sort_order
               ) AS items
        FROM  resource_flows rf
        JOIN  items i ON i.id = rf.item_id
        WHERE rf.parent_type = 0
          AND rf.recipe_id   = r.id
          AND rf.direction   = 1
    ) out ON TRUE

    LEFT JOIN LATERAL (
        SELECT json_agg(
                   json_build_object(
                       'item',         bm.item,
                       'rate_per_min', bm.rate_per_min
                   ) ORDER BY bm.item
               ) AS items
        FROM  building_maintenance bm
        WHERE bm.building_id = b.id
    ) mnt ON TRUE

    UNION ALL

    SELECT
        'complex'                       AS node_type,
        c.id                            AS node_id,
        c.name                          AS name,
        NULL                            AS machine_name,
        NULL                            AS cycle_time_s,
        c.total_workers                 AS workers,
        c.total_electricity_kw          AS electricity_kw,
        c.deprecated,
        inp.items                       AS inputs,
        out.items                       AS outputs,
        mnt_cx.items                    AS maintenance

    FROM complexes c

    LEFT JOIN LATERAL (
        SELECT json_agg(
                   json_build_object('item', i.name, 'qty_per_min', rf.qty_per_min)
                   ORDER BY i.name
               ) AS items
        FROM  resource_flows rf
        JOIN  items i ON i.id = rf.item_id
        WHERE rf.parent_type = 1
          AND rf.complex_id  = c.id
          AND rf.direction   = 0
    ) inp ON TRUE

    LEFT JOIN LATERAL (
        SELECT json_agg(
                   json_build_object('item', i.name, 'qty_per_min', rf.qty_per_min)
                   ORDER BY i.name
               ) AS items
        FROM  resource_flows rf
        JOIN  items i ON i.id = rf.item_id
        WHERE rf.parent_type = 1
          AND rf.complex_id  = c.id
          AND rf.direction   = 1
    ) out ON TRUE

    LEFT JOIN LATERAL (
        SELECT json_agg(
                   json_build_object('item', cm2.item, 'rate_per_min', cm2.rate_per_min)
                   ORDER BY cm2.item
               ) AS items
        FROM  complex_maintenance cm2
        WHERE cm2.complex_id = c.id
    ) mnt_cx ON TRUE

) _nodes
ORDER BY COALESCE(machine_name, name)
"""


def _parse_row(row: dict) -> dict:
    """Нормализует JSON-поля и приводит типы к сериализуемым."""
    import decimal

    row = dict(row)
    for f in ("inputs", "outputs", "maintenance"):
        v = row[f]
        if v is None:
            row[f] = []
        elif isinstance(v, str):
            row[f] = json.loads(v)
        # Если psycopg2 уже разобрал JSON в Python-список — оставляем,
        # но конвертируем Decimal внутри в float (json.dumps не умеет Decimal)
        if isinstance(row[f], list):
            row[f] = [
                {k: float(val) if isinstance(val, decimal.Decimal) else val
                 for k, val in item.items()}
                for item in row[f]
            ]
    for f in ("cycle_time_s", "workers", "electricity_kw"):
        if row[f] is not None:
            row[f] = float(row[f])
    # Явно bool, чтобы JSON не слал null
    row["deprecated"] = bool(row.get("deprecated"))
    return row


# ─────────────────────────────────────────────────────────────────
# Маршруты
# ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/nodes")
def api_nodes():
    q           = request.args.get("q",       "").strip().lower()
    type_filter = request.args.get("type",    "all")   # all|recipe|complex
    # deprecated=false → вернуть все (фильтрация на клиенте)
    hide_hidden = request.args.get("hidden",  "false") == "true"

    try:
        with get_db() as con:
            with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(NODES_SQL)
                rows = [_parse_row(r) for r in cur.fetchall()]
    except Exception as e:
        import traceback
        msg = getattr(e, "pgerror", None) or repr(e)
        return jsonify({"error": msg, "detail": traceback.format_exc()}), 500

    result = []
    for row in rows:
        if type_filter != "all" and row["node_type"] != type_filter:
            continue
        if hide_hidden and row["deprecated"]:
            continue
        if q:
            haystack = " ".join(filter(None, [
                row.get("machine_name") or "",
                row.get("name")         or "",
                " ".join(x["item"] for x in row["inputs"]),
                " ".join(x["item"] for x in row["outputs"]),
            ])).lower()
            if q not in haystack:
                continue
        result.append(row)

    return jsonify(result)


@app.route("/api/node/<node_type>/<int:node_id>/hidden", methods=["PATCH"])
def toggle_hidden(node_type: str, node_id: int):
    if node_type not in ("recipe", "complex"):
        return jsonify({"error": "invalid node_type"}), 400
    data   = request.get_json(silent=True) or {}
    hidden = data.get("hidden")
    if not isinstance(hidden, bool):
        return jsonify({"error": "hidden must be bool"}), 400

    table = "recipes" if node_type == "recipe" else "complexes"
    try:
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute(
                    f"UPDATE {table} SET deprecated = %s WHERE id = %s",
                    (hidden, node_id)
                )
            con.commit()
    except Exception as e:
        return jsonify({"error": repr(e)}), 500
    return jsonify({"ok": True, "node_type": node_type, "node_id": node_id, "hidden": hidden})


@app.route("/api/nodes/hidden/batch", methods=["PATCH"])
def batch_hidden():
    data    = request.get_json(silent=True) or {}
    hidden  = data.get("hidden")
    items   = data.get("items", [])   # [{node_type, node_id}, ...]
    if not isinstance(hidden, bool):
        return jsonify({"error": "hidden must be bool"}), 400

    recipe_ids  = [x["node_id"] for x in items if x.get("node_type") == "recipe"]
    complex_ids = [x["node_id"] for x in items if x.get("node_type") == "complex"]

    try:
        with get_db() as con:
            with con.cursor() as cur:
                if recipe_ids:
                    cur.execute(
                        "UPDATE recipes  SET deprecated = %s WHERE id = ANY(%s)",
                        (hidden, recipe_ids)
                    )
                if complex_ids:
                    cur.execute(
                        "UPDATE complexes SET deprecated = %s WHERE id = ANY(%s)",
                        (hidden, complex_ids)
                    )
            con.commit()
    except Exception as e:
        return jsonify({"error": repr(e)}), 500
    return jsonify({"ok": True, "hidden": hidden,
                    "recipes": len(recipe_ids), "complexes": len(complex_ids)})


@app.route("/api/node/<node_type>/<int:node_id>")
def api_node_detail(node_type: str, node_id: int):
    """Данные одного рецепта или комплекса по типу и id."""
    if node_type not in ("recipe", "complex"):
        return jsonify({"error": "invalid type"}), 400
    try:
        with get_db() as con:
            with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(NODES_SQL)
                rows = [_parse_row(r) for r in cur.fetchall()]
        row = next(
            (r for r in rows if r["node_type"] == node_type and r["node_id"] == node_id),
            None,
        )
        if not row:
            return jsonify({"error": "not found"}), 404
        return jsonify(row)
    except Exception as e:
        import traceback
        return jsonify({"error": repr(e), "detail": traceback.format_exc()}), 500


@app.route("/api/icons/missing")
def api_icons_missing():
    """Список предметов без локальных иконок в static/icons/."""
    import os
    icons_dir = os.path.join(os.path.dirname(__file__), "static", "icons")
    try:
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute("SELECT name FROM items ORDER BY name")
                items = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT DISTINCT item FROM building_maintenance ORDER BY item")
                maint = [r[0] for r in cur.fetchall()]

        all_names = sorted(set(items) | set(maint))
        missing, found = [], []
        for name in all_names:
            path = os.path.join(icons_dir, name.replace(" ", "_") + ".png")
            if os.path.exists(path) and os.path.getsize(path) >= 100:
                found.append(name)
            else:
                missing.append(name)

        return jsonify({
            "total":   len(all_names),
            "found":   len(found),
            "missing": missing,
        })
    except Exception as e:
        return jsonify({"error": repr(e)}), 500


@app.route("/api/debug")
def api_debug():
    result = {"db": DB.copy(), "tables": [], "counts": {}, "error": None}
    result["db"].pop("password", None)
    try:
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT table_name
                    FROM   information_schema.tables
                    WHERE  table_schema = 'public'
                    ORDER  BY table_name
                """)
                result["tables"] = [r[0] for r in cur.fetchall()]

                # Количество строк в ключевых таблицах
                for tbl in ("recipes", "buildings", "building_maintenance",
                            "items", "resource_flows", "complexes"):
                    cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                    result["counts"][tbl] = cur.fetchone()[0]

                # Примеры данных техобслуживания
                cur.execute("""
                    SELECT b.name, bm.item, bm.rate_per_min
                    FROM   building_maintenance bm
                    JOIN   buildings b ON b.id = bm.building_id
                    LIMIT  5
                """)
                result["maintenance_sample"] = [
                    {"building": r[0], "item": r[1], "rate_per_min": float(r[2])}
                    for r in cur.fetchall()
                ]

                # Рецепты без привязки к зданию
                cur.execute("""
                    SELECT COUNT(*) FROM recipes WHERE machine_id IS NULL
                """)
                result["recipes_no_building"] = cur.fetchone()[0]

    except Exception as e:
        result["error"] = getattr(e, "pgerror", None) or repr(e)
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────
# Редактор комплексов — страницы
# ─────────────────────────────────────────────────────────────────

@app.route("/complex/new")
def complex_new():
    return render_template("complex_editor.html", complex_id="null")


@app.route("/complex/<int:complex_id>/edit")
def complex_edit(complex_id: int):
    return render_template("complex_editor.html", complex_id=complex_id)


# ─────────────────────────────────────────────────────────────────
# API: пикер рецептов/комплексов для ресурса
# ─────────────────────────────────────────────────────────────────

@app.route("/api/nodes/for-resource")
def api_nodes_for_resource():
    """
    ?item=X&direction=produces|consumes&type=all|recipe|complex&hidden=false|true
    Возвращает рецепты/комплексы, у которых X — выход (produces) или вход (consumes).
    """
    item      = request.args.get("item", "").strip()
    direction = request.args.get("direction", "produces")   # produces|consumes
    type_flt  = request.args.get("type", "all")
    show_hid  = request.args.get("hidden", "false") == "true"

    if not item:
        return jsonify([])

    try:
        with get_db() as con:
            with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(NODES_SQL)
                rows = [_parse_row(r) for r in cur.fetchall()]
    except Exception as e:
        import traceback
        return jsonify({"error": repr(e), "detail": traceback.format_exc()}), 500

    result = []
    for row in rows:
        if not show_hid and row["deprecated"]:
            continue
        if type_flt != "all" and row["node_type"] != type_flt:
            continue
        check = row["outputs"] if direction == "produces" else row["inputs"]
        if any(x["item"] == item for x in check):
            result.append(row)

    return jsonify(result)


# ─────────────────────────────────────────────────────────────────
# API: граф комплекса (для редактора)
# ─────────────────────────────────────────────────────────────────

def _parse_json_list(v):
    import decimal
    if v is None:
        return []
    if isinstance(v, str):
        v = json.loads(v)
    if isinstance(v, list):
        return [
            {k: float(val) if isinstance(val, decimal.Decimal) else val
             for k, val in item.items()}
            for item in v
        ]
    return []


@app.route("/api/complex/<int:complex_id>/graph")
def api_complex_graph(complex_id: int):
    try:
        with get_db() as con:
            with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT id, name, description FROM complexes WHERE id = %s",
                    (complex_id,)
                )
                cx = cur.fetchone()
                if not cx:
                    return jsonify({"error": "not found"}), 404

                # Члены комплекса + полные данные рецептов/подкомплексов
                cur.execute("""
                    SELECT
                        cm.id, cm.child_type, cm.child_id,
                        cm.multiplier, cm.pos_x, cm.pos_y,
                        cm.efficiency, cm.idle_item, cm.idle_direction, cm.is_manual_partial,
                        cm.external_ports,
                        -- рецепт
                        r.machine_name,
                        b.workers, b.electricity_kw,
                        -- подкомплекс
                        c2.name  AS complex_name,
                        -- ресурсы рецепта
                        inp.items  AS inputs,
                        out.items  AS outputs,
                        mnt.items  AS maintenance
                    FROM complex_members cm
                    LEFT JOIN recipes   r  ON r.id  = cm.recipe_id
                    LEFT JOIN buildings b  ON b.id  = r.machine_id
                    LEFT JOIN complexes c2 ON c2.id = cm.child_complex_id

                    LEFT JOIN LATERAL (
                        SELECT json_agg(json_build_object(
                            'item', i.name, 'qty_per_min', rf.qty_per_min)
                            ORDER BY rf.sort_order) AS items
                        FROM resource_flows rf JOIN items i ON i.id = rf.item_id
                        WHERE rf.parent_type = 0 AND rf.recipe_id = cm.recipe_id
                          AND rf.direction = 0
                    ) inp ON (cm.child_type = 0)

                    LEFT JOIN LATERAL (
                        SELECT json_agg(json_build_object(
                            'item', i.name, 'qty_per_min', rf.qty_per_min)
                            ORDER BY rf.sort_order) AS items
                        FROM resource_flows rf JOIN items i ON i.id = rf.item_id
                        WHERE rf.parent_type = 0 AND rf.recipe_id = cm.recipe_id
                          AND rf.direction = 1
                    ) out ON (cm.child_type = 0)

                    LEFT JOIN LATERAL (
                        SELECT json_agg(json_build_object(
                            'item', bm.item, 'rate_per_min', bm.rate_per_min)
                            ORDER BY bm.item) AS items
                        FROM building_maintenance bm WHERE bm.building_id = b.id
                    ) mnt ON (cm.child_type = 0)

                    WHERE cm.complex_id = %s
                    ORDER BY cm.id
                """, (complex_id,))
                members = cur.fetchall()

                # Рёбра
                cur.execute("""
                    SELECT id, from_member_id, to_member_id, resource_item, lcm_mode
                    FROM   complex_edges
                    WHERE  complex_id = %s
                    ORDER  BY id
                """, (complex_id,))
                edges = [dict(e) for e in cur.fetchall()]

                # Ресурсы подкомплексов (из resource_flows parent_type=1)
                sub_ids = [m["child_id"] for m in members if m["child_type"] == 1]
                sub_flows: dict[int, dict] = {}
                if sub_ids:
                    cur.execute("""
                        SELECT rf.complex_id, rf.direction, i.name AS item,
                               rf.qty_per_min
                        FROM   resource_flows rf
                        JOIN   items i ON i.id = rf.item_id
                        WHERE  rf.parent_type = 1
                          AND  rf.complex_id  = ANY(%s)
                    """, (sub_ids,))
                    for row in cur.fetchall():
                        cid = row["complex_id"]
                        if cid not in sub_flows:
                            sub_flows[cid] = {"inputs": [], "outputs": []}
                        key = "inputs" if row["direction"] == 0 else "outputs"
                        sub_flows[cid][key].append({
                            "item": row["item"],
                            "qty_per_min": float(row["qty_per_min"]),
                        })

        nodes = []
        for m in members:
            m = dict(m)
            is_complex = (m["child_type"] == 1)
            if is_complex:
                sf = sub_flows.get(m["child_id"], {"inputs": [], "outputs": []})
                inp_list = sf["inputs"]
                out_list = sf["outputs"]
                mnt_list = []
            else:
                inp_list = _parse_json_list(m["inputs"])
                out_list = _parse_json_list(m["outputs"])
                mnt_list = _parse_json_list(m["maintenance"])

            import decimal
            def _f(v):
                return float(v) if isinstance(v, decimal.Decimal) else v

            nodes.append({
                "id":             m["id"],
                "node_type":      "complex" if is_complex else "recipe",
                "node_ref_id":    m["child_id"],
                "count":          int(m["multiplier"]),
                "pos_x":          m["pos_x"],
                "pos_y":          m["pos_y"],
                "efficiency":     float(m["efficiency"]) if m["efficiency"] is not None else 1.0,
                "idle_item":         m["idle_item"],
                "idle_direction":    m["idle_direction"],
                "is_manual_partial": bool(m["is_manual_partial"]),
                "external_ports":    json.loads(m["external_ports"]) if m.get("external_ports") else [],
                "label":          m["machine_name"] or m["complex_name"] or "?",
                "workers":        _f(m["workers"]) if m["workers"] is not None else None,
                "electricity_kw": _f(m["electricity_kw"]) if m["electricity_kw"] is not None else None,
                "inputs":         inp_list,
                "outputs":        out_list,
                "maintenance":    mnt_list,
            })

        return jsonify({
            "id":          cx["id"],
            "name":        cx["name"],
            "description": cx["description"],
            "nodes":       nodes,
            "edges":       edges,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": repr(e), "detail": traceback.format_exc()}), 500


# ─────────────────────────────────────────────────────────────────
# API: сохранение комплекса
# ─────────────────────────────────────────────────────────────────

def _save_complex_graph(con, complex_id, data):
    """
    Сохраняет граф комплекса. Возвращает complex_id.
    data = {name, nodes: [{_id, node_type, node_ref_id, count, pos_x, pos_y}],
                   edges: [{from_node_id, to_node_id, resource_item, lcm_mode}]}
    """
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")

    nodes_data = data.get("nodes", [])
    edges_data = data.get("edges", [])

    with con.cursor() as cur:
        if complex_id:
            cur.execute(
                "UPDATE complexes SET name = %s WHERE id = %s RETURNING id",
                (name, complex_id)
            )
            if not cur.fetchone():
                raise ValueError("complex not found")
        else:
            cur.execute(
                "INSERT INTO complexes (name) VALUES (%s) RETURNING id",
                (name,)
            )
            complex_id = cur.fetchone()[0]

        # Удаляем старые рёбра и узлы (каскад удалит рёбра автоматически)
        cur.execute("DELETE FROM complex_members WHERE complex_id = %s", (complex_id,))

        # Вставляем узлы, строим маппинг клиентский _id → db id
        id_map: dict[str, int] = {}
        for i, nd in enumerate(nodes_data):
            child_type     = 0 if nd["node_type"] == "recipe" else 1
            ref_id         = int(nd["node_ref_id"])
            is_manual_partial = bool(nd.get("is_manual_partial", False))
            count          = max(1, int(round(float(nd.get("count", 1)))))
            pos_x          = int(nd.get("pos_x", i * 380))
            pos_y          = int(nd.get("pos_y", 100))
            efficiency     = max(0.0001, min(1.0, float(nd.get("efficiency", 1.0))))
            idle_item      = nd.get("idle_item") or None
            idle_direction = nd.get("idle_direction") or None
            ext_ports_raw  = nd.get("external_ports") or []
            external_ports = json.dumps(ext_ports_raw) if ext_ports_raw else None

            cur.execute("""
                INSERT INTO complex_members
                    (complex_id, child_type, child_id,
                     recipe_id, child_complex_id, multiplier, pos_x, pos_y,
                     efficiency, idle_item, idle_direction, is_manual_partial,
                     external_ports)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                complex_id, child_type, ref_id,
                ref_id if child_type == 0 else None,
                ref_id if child_type == 1 else None,
                count, pos_x, pos_y,
                efficiency, idle_item, idle_direction, is_manual_partial,
                external_ports,
            ))
            db_id = cur.fetchone()[0]
            id_map[nd["_id"]] = db_id

        # Вставляем рёбра
        for ed in edges_data:
            from_db = id_map.get(ed["from_node_id"])
            to_db   = id_map.get(ed["to_node_id"])
            if from_db is None or to_db is None:
                continue
            cur.execute("""
                INSERT INTO complex_edges
                    (complex_id, from_member_id, to_member_id, resource_item, lcm_mode)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                complex_id, from_db, to_db,
                ed["resource_item"], bool(ed.get("lcm_mode", False)),
            ))

        # Пересчёт агрегатов (workers, electricity, resource_flows, maintenance)
        cur.execute("SELECT recalculate_complex(%s)", (complex_id,))

        # ── Очистка шумовых потоков после пересчёта ──────────────────────────
        # 1. Idle-порт (фиолетовый): пользователь явно закрыл этот ресурс в ноль,
        #    любой остаток — погрешность умножения на дробный efficiency.
        cur.execute("""
            DELETE FROM resource_flows rf
            USING  items i
            WHERE  rf.parent_type = 1
              AND  rf.complex_id  = %s
              AND  i.id           = rf.item_id
              AND  i.name IN (
                  SELECT idle_item
                  FROM   complex_members
                  WHERE  complex_id = %s
                    AND  idle_item IS NOT NULL
              )
        """, (complex_id, complex_id))

        # 2. Ручной дробный режим: дробный count даёт погрешность во всех ресурсах узла;
        #    при больших объёмах хвост легко >0.1. В игре нет ценных потоков < 1/мин,
        #    поэтому при наличии хотя бы одного ручного узла отбрасываем всё < 1.
        cur.execute("""
            DELETE FROM resource_flows
            WHERE  parent_type = 1
              AND  complex_id  = %s
              AND  qty_per_min < 1
              AND  EXISTS (
                  SELECT 1 FROM complex_members
                  WHERE  complex_id        = %s
                    AND  is_manual_partial = TRUE
              )
        """, (complex_id, complex_id))

    return complex_id


@app.route("/api/complex", methods=["POST"])
def api_complex_create():
    data = request.get_json(silent=True) or {}
    try:
        with get_db() as con:
            cid = _save_complex_graph(con, None, data)
            con.commit()
        return jsonify({"ok": True, "id": cid}), 201
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Комплекс с таким именем уже существует"}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        return jsonify({"error": repr(e), "detail": traceback.format_exc()}), 500


@app.route("/api/complex/<int:complex_id>", methods=["PUT"])
def api_complex_update(complex_id: int):
    data = request.get_json(silent=True) or {}
    try:
        with get_db() as con:
            _save_complex_graph(con, complex_id, data)
            con.commit()
        return jsonify({"ok": True, "id": complex_id})
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Комплекс с таким именем уже существует"}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        return jsonify({"error": repr(e), "detail": traceback.format_exc()}), 500


@app.route("/api/complex/<int:complex_id>/members")
def api_complex_members(complex_id: int):
    """Список машин / подкомплексов, входящих в комплекс (для раскрытия строки)."""
    try:
        with get_db() as con:
            with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        CASE cm.child_type WHEN 0 THEN 'recipe' ELSE 'complex' END AS node_type,
                        COALESCE(r.machine_name, c2.name, '?')   AS label,
                        cm.multiplier                             AS count,
                        COALESCE(cm.efficiency, 1.0)             AS efficiency,
                        COALESCE(b.workers,           c2.total_workers)        AS workers,
                        COALESCE(b.electricity_kw,    c2.total_electricity_kw) AS electricity_kw
                    FROM  complex_members cm
                    LEFT  JOIN recipes   r  ON r.id  = cm.recipe_id
                    LEFT  JOIN buildings b  ON b.id  = r.machine_id
                    LEFT  JOIN complexes c2 ON c2.id = cm.child_complex_id
                    WHERE cm.complex_id = %s
                    ORDER BY label
                """, (complex_id,))
                rows = cur.fetchall()
        import decimal
        result = []
        for row in rows:
            row = dict(row)
            for f in ('workers', 'electricity_kw', 'efficiency', 'count'):
                if row[f] is not None and isinstance(row[f], decimal.Decimal):
                    row[f] = float(row[f])
            result.append(row)
        return jsonify(result)
    except Exception as e:
        import traceback
        return jsonify({'error': repr(e), 'detail': traceback.format_exc()}), 500


@app.route("/api/complex/<int:complex_id>", methods=["DELETE"])
def api_complex_delete(complex_id: int):
    try:
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute("DELETE FROM complexes WHERE id = %s", (complex_id,))
            con.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": repr(e)}), 500


if __name__ == "__main__":
    _ensure_schema()
    app.run(debug=True, port=5000)
