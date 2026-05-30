"""
CoI Game Data Diff — генератор инкрементального SQL-патча.

Что делает:
  1. Загружает recipes.json, items.json, buildings.json
  2. Применяет патчи из coi_fixes.json
  3. Подключается к PostgreSQL и читает текущее состояние
  4. Сравнивает: новое/изменённое/удалённое
  5. Пишет pending_update.sql — готов к ревью и запуску

Запуск: python coi_diff.py
        (или через run_update.bat)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.exit("psycopg2 не установлен: pip install psycopg2-binary")

# ── Пути ──────────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).parent
RECIPES_JSON  = ROOT / "recipes.json"
ITEMS_JSON    = ROOT / "items.json"
BUILDINGS_JSON = ROOT / "buildings.json"
FIXES_JSON    = ROOT / "coi_fixes.json"
OUT_SQL       = ROOT / "pending_update.sql"

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/coi_public",
)

# Поля рецепта, изменение которых считается «значимым» (триггер UPDATE)
RECIPE_CMP_FIELDS = {"cycle_time_s"}
# Поля здания, изменение которых считается «значимым»
BUILDING_CMP_FIELDS = {"workers", "electricity_kw"}


# ── Загрузка JSON ──────────────────────────────────────────────────────────────

def load_json(path: Path) -> list | dict:
    if not path.exists():
        print(f"  [warn] {path.name} не найден — пропускаем")
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def apply_fixes(recipes: list[dict], fixes: dict) -> list[dict]:
    """Применить патчи из coi_fixes.json поверх распарсенных рецептов."""
    by_id = fixes.get("by_recipe_id", {})
    count = 0
    for r in recipes:
        rid = r.get("recipe_id")
        if rid and rid in by_id:
            patch = {k: v for k, v in by_id[rid].items() if not k.startswith("_")}
            before = {k: r.get(k) for k in patch}
            r.update(patch)
            print(f"  [fix] {rid}: {before} -> {patch}")
            count += 1
    if count:
        print(f"  Патчей применено: {count}")
    return recipes


# ── Чтение текущего состояния БД ───────────────────────────────────────────────

def db_connect():
    return psycopg2.connect(DATABASE_URL)


def ensure_wiki_id_column(cur):
    """Добавить wiki_id в recipes если ещё нет (идемпотентно)."""
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'recipes' AND column_name = 'wiki_id'
    """)
    if not cur.fetchone():
        print("  [migration] Добавляем колонку recipes.wiki_id ...")
        cur.execute("ALTER TABLE recipes ADD COLUMN wiki_id TEXT")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_recipes_wiki_id ON recipes(wiki_id) WHERE wiki_id IS NOT NULL")


def read_db_state(cur) -> tuple[dict, dict, dict, dict]:
    """
    Возвращает (db_items, db_buildings, db_recipes, db_flows).
      db_items     : {name: id}
      db_buildings : {name: {id, workers, electricity_kw}}
      db_recipes   : {wiki_id: {id, machine_name, cycle_time_s, machine_id}}
      db_flows     : {recipe_id: [(item_name, direction, qty_cycle, qty_min, sort)]}
    """
    cur.execute("SELECT id, name FROM items")
    db_items = {r[1]: r[0] for r in cur.fetchall()}

    cur.execute("SELECT id, name, workers, electricity_kw FROM buildings")
    db_buildings = {
        r[1]: {"id": r[0], "workers": r[2], "electricity_kw": float(r[3]) if r[3] is not None else None}
        for r in cur.fetchall()
    }

    cur.execute("SELECT id, wiki_id, machine_name, cycle_time_s, machine_id FROM recipes WHERE wiki_id IS NOT NULL")
    db_recipes = {
        r[1]: {
            "id":           r[0],
            "machine_name": r[2],
            "cycle_time_s": float(r[3]) if r[3] is not None else None,
            "machine_id":   r[4],
        }
        for r in cur.fetchall()
    }

    cur.execute("""
        SELECT rf.recipe_id, i.name, rf.direction, rf.qty_per_cycle, rf.qty_per_min, rf.sort_order
        FROM resource_flows rf JOIN items i ON i.id = rf.item_id
        WHERE rf.parent_type = 0
        ORDER BY rf.recipe_id, rf.sort_order, rf.direction
    """)
    db_flows: dict[int, list] = {}
    for recipe_id, item_name, direction, qty_cycle, qty_min, sort in cur.fetchall():
        db_flows.setdefault(recipe_id, []).append(
            (item_name, direction, qty_cycle, int(qty_min * 10000) if qty_min else None, sort)
        )

    return db_items, db_buildings, db_recipes, db_flows


# ── Генерация SQL ──────────────────────────────────────────────────────────────

def _sql_val(v):
    """Безопасный SQL-литерал (только для числовых/None/строк внутри f-строк)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    # строки — одинарные кавычки с эскейпом
    return "'" + str(v).replace("'", "''") + "'"


def generate_sql(
    recipes_new: list[dict],
    items_all: list[str],
    buildings_all: list[dict],
    db_items: dict,
    db_buildings: dict,
    db_recipes: dict,
    db_flows: dict,
) -> str:
    lines: list[str] = []
    stats = {"items": 0, "buildings_new": 0, "buildings_upd": 0,
             "recipes_new": 0, "recipes_upd": 0, "recipes_dep": 0, "flows_upd": 0}

    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines += [
        f"-- ════════════════════════════════════════════════════════════",
        f"-- CoI game-data incremental patch  {ts}",
        f"-- Сгенерирован coi_diff.py — ПРОВЕРЬ ПЕРЕД ЗАПУСКОМ",
        f"-- ════════════════════════════════════════════════════════════",
        "",
        "BEGIN;",
        "",
    ]

    # ── 1. Items ────────────────────────────────────────────────────────
    new_items = [name for name in items_all if name not in db_items]
    if new_items:
        lines.append(f"-- [{len(new_items)} новых предметов]")
        for name in sorted(new_items):
            lines.append(f"INSERT INTO items (name) VALUES ({_sql_val(name)}) ON CONFLICT (name) DO NOTHING;")
            stats["items"] += 1
        lines.append("")

    # ── 2. Buildings ─────────────────────────────────────────────────────
    new_blds, upd_blds = [], []
    for b in buildings_all:
        name = b["name"]
        if name not in db_buildings:
            new_blds.append(b)
        else:
            db = db_buildings[name]
            changed = {}
            if b.get("workers")        != db["workers"]:        changed["workers"]        = b.get("workers")
            if b.get("electricity_kw") != db["electricity_kw"]: changed["electricity_kw"] = b.get("electricity_kw")
            if changed:
                upd_blds.append((name, changed))

    if new_blds:
        lines.append(f"-- [{len(new_blds)} новых зданий]")
        for b in new_blds:
            lines.append(
                f"INSERT INTO buildings (name, workers, electricity_kw, footprint, designation)"
                f" VALUES ({_sql_val(b['name'])}, {_sql_val(b.get('workers'))}, "
                f"{_sql_val(b.get('electricity_kw'))}, {_sql_val(b.get('footprint'))}, "
                f"{_sql_val(b.get('designation'))})"
                f" ON CONFLICT (name) DO NOTHING;"
            )
            stats["buildings_new"] += 1
        lines.append("")

    if upd_blds:
        lines.append(f"-- [{len(upd_blds)} изменившихся зданий]")
        for name, changed in upd_blds:
            sets = ", ".join(f"{col} = {_sql_val(val)}" for col, val in changed.items())
            lines.append(f"UPDATE buildings SET {sets} WHERE name = {_sql_val(name)};")
            stats["buildings_upd"] += 1
        lines.append("")

    # ── 3. Recipes ────────────────────────────────────────────────────────
    json_wiki_ids = {r["recipe_id"] for r in recipes_new if r.get("recipe_id")}

    new_recipes, upd_recipes = [], []
    for r in recipes_new:
        wiki_id = r.get("recipe_id")
        if not wiki_id:
            continue
        machine_name = r.get("machine", "")
        cycle = r.get("cycle_time_s")

        if wiki_id not in db_recipes:
            new_recipes.append(r)
        else:
            db_r = db_recipes[wiki_id]
            changed = {}
            if cycle != db_r["cycle_time_s"]:
                changed["cycle_time_s"] = cycle
            if machine_name != db_r["machine_name"]:
                changed["machine_name"] = machine_name
            if changed:
                upd_recipes.append((wiki_id, db_r["id"], changed, r))

    # Рецепты, которые есть в БД, но исчезли с Вики → deprecated
    dep_recipes = [
        (wiki_id, info["id"])
        for wiki_id, info in db_recipes.items()
        if wiki_id not in json_wiki_ids
    ]

    if new_recipes:
        lines.append(f"-- [{len(new_recipes)} новых рецептов]")
        for r in new_recipes:
            wiki_id      = r["recipe_id"]
            machine_name = r.get("machine", "")
            cycle        = r.get("cycle_time_s")
            # machine_id подставляется подзапросом
            lines.append(
                f"INSERT INTO recipes (wiki_id, machine_name, machine_id, cycle_time_s)"
                f" VALUES ({_sql_val(wiki_id)}, {_sql_val(machine_name)},"
                f" (SELECT id FROM buildings WHERE name = {_sql_val(machine_name)} LIMIT 1),"
                f" {_sql_val(cycle)})"
                f" ON CONFLICT (wiki_id) WHERE wiki_id IS NOT NULL DO NOTHING;"
            )
            # Потоки ресурсов для нового рецепта
            flows = _recipe_flows_sql(wiki_id, r.get("inputs", []), r.get("outputs", []))
            if flows:
                lines.append(f"-- flows for {wiki_id}")
                lines += flows
            stats["recipes_new"] += 1
        lines.append("")

    if upd_recipes:
        lines.append(f"-- [{len(upd_recipes)} изменившихся рецептов]")
        for wiki_id, db_id, changed, r in upd_recipes:
            sets = ", ".join(f"{col} = {_sql_val(val)}" for col, val in changed.items())
            lines.append(f"-- {wiki_id}: {changed}")
            lines.append(f"UPDATE recipes SET {sets} WHERE wiki_id = {_sql_val(wiki_id)};")
            # Пересчитать qty_per_min если изменился cycle_time_s
            if "cycle_time_s" in changed:
                flows = _recipe_flows_sql(wiki_id, r.get("inputs", []), r.get("outputs", []))
                if flows:
                    lines.append(f"DELETE FROM resource_flows WHERE parent_type = 0 AND recipe_id = {db_id};")
                    lines.append(f"-- new flows for {wiki_id}")
                    lines += flows
                    stats["flows_upd"] += 1
            stats["recipes_upd"] += 1
        lines.append("")

    if dep_recipes:
        lines.append(f"-- [{len(dep_recipes)} рецептов исчезли с вики → deprecated]")
        lines.append("-- ВНИМАНИЕ: проверь что это не ошибка парсера перед применением!")
        for wiki_id, db_id in dep_recipes:
            lines.append(f"-- UPDATE recipes SET deprecated = TRUE WHERE wiki_id = {_sql_val(wiki_id)};  -- раскомментировать если уверен")
            stats["recipes_dep"] += 1
        lines.append("")

    # ── Итоги ──────────────────────────────────────────────────────────────
    lines += [
        "COMMIT;",
        "",
        "-- ── Итоги ────────────────────────────────────────────────────",
        f"-- Новых предметов:    {stats['items']}",
        f"-- Новых зданий:       {stats['buildings_new']}",
        f"-- Изменено зданий:    {stats['buildings_upd']}",
        f"-- Новых рецептов:     {stats['recipes_new']}",
        f"-- Изменено рецептов:  {stats['recipes_upd']}",
        f"-- Deprecated рецептов (закоммент.): {stats['recipes_dep']}",
    ]

    return "\n".join(lines) + "\n"


def _recipe_flows_sql(wiki_id: str, inputs: list, outputs: list) -> list[str]:
    """SQL для INSERT resource_flows нового/пересчитанного рецепта."""
    lines = []
    all_flows = [(inp, 0) for inp in inputs] + [(out, 1) for out in outputs]
    for sort_order, (flow, direction) in enumerate(all_flows):
        item  = flow["item"]
        qty_c = flow.get("qty_per_cycle")
        qty_m = flow.get("qty_per_min")
        lines.append(
            f"INSERT INTO resource_flows"
            f" (parent_type, parent_id, recipe_id, item_id, direction, qty_per_cycle, qty_per_min, sort_order)"
            f" SELECT 0, r.id, r.id,"
            f" (SELECT id FROM items WHERE name = {_sql_val(item)}),"
            f" {direction}, {_sql_val(qty_c)}, {_sql_val(qty_m)}, {sort_order}"
            f" FROM recipes r WHERE r.wiki_id = {_sql_val(wiki_id)}"
            f" ON CONFLICT (parent_type, parent_id, item_id, direction) DO UPDATE"
            f"   SET qty_per_cycle = EXCLUDED.qty_per_cycle, qty_per_min = EXCLUDED.qty_per_min;"
        )
    return lines


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("CoI Diff — генератор инкрементального патча")
    print("=" * 55)

    # 1. Загрузить JSON
    print("\n[1/4] Загрузка JSON...")
    recipes   = load_json(RECIPES_JSON)
    items_raw = load_json(ITEMS_JSON)
    buildings = load_json(BUILDINGS_JSON)
    fixes     = load_json(FIXES_JSON) if FIXES_JSON.exists() else {}

    if not recipes:
        sys.exit("recipes.json пустой или не найден — запустите coi_parser.py сначала")

    items_list = items_raw if isinstance(items_raw, list) else []
    print(f"  Рецептов: {len(recipes)}, предметов: {len(items_list)}, зданий: {len(buildings)}")

    # 2. Применить фиксы
    print("\n[2/4] Применяем патчи из coi_fixes.json...")
    recipes = apply_fixes(recipes, fixes)

    # 3. Читать БД
    print(f"\n[3/4] Подключение к БД: {DATABASE_URL.split('@')[-1]}...")
    try:
        conn = db_connect()
    except Exception as e:
        sys.exit(f"Не удалось подключиться к БД: {e}")

    with conn:
        with conn.cursor() as cur:
            ensure_wiki_id_column(cur)
            db_items, db_buildings, db_recipes, db_flows = read_db_state(cur)

    print(f"  В БД: {len(db_items)} предметов, {len(db_buildings)} зданий, {len(db_recipes)} рецептов с wiki_id")
    conn.close()

    # 4. Генерировать SQL
    print("\n[4/4] Сравниваем и генерируем SQL...")
    sql = generate_sql(recipes, items_list, buildings, db_items, db_buildings, db_recipes, db_flows)

    OUT_SQL.write_text(sql, encoding="utf-8")
    print(f"\n  -> {OUT_SQL.name}")

    # Краткое резюме
    new_items   = sum(1 for n in items_list if n not in db_items)
    new_recipes = sum(1 for r in recipes if r.get("recipe_id") and r["recipe_id"] not in db_recipes)
    dep_recipes = sum(1 for wid in db_recipes if wid not in {r.get("recipe_id") for r in recipes})
    print(f"\n  Новых предметов:  {new_items}")
    print(f"  Новых рецептов:   {new_recipes}")
    print(f"  Пропали с вики:   {dep_recipes}  (закомментированы в SQL — проверь вручную)")
    print(f"\nОткрой {OUT_SQL.name}, проверь, затем запусти run_apply.bat")


if __name__ == "__main__":
    main()
