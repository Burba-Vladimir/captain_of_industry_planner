"""
Актуализация wiki_id в таблице recipes.

Сопоставляет рецепты из recipes.json (поле recipe_id = wiki_id)
с записями в БД по ключу:
    (machine_name, frozenset входов (item, qty_per_cycle), frozenset выходов)

Запуск:
    python backfill_wiki_ids.py            # применить
    python backfill_wiki_ids.py --dry-run  # только показать результат
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent


# ── Ключ сопоставления ───────────────────────────────────────────

def _norm_qty(q) -> float | None:
    """Нормализует qty: float или None (null-qty сохраняем, не отфильтровываем)."""
    return float(q) if q is not None else None


def _fingerprint_json(r: dict, with_cycle: bool = False) -> tuple:
    """Fingerprint рецепта из recipes.json. null-qty сохраняется как None.
    with_cycle=True — добавляет cycle_time_s для разрешения конфликтов."""
    ins  = frozenset((x["item"], _norm_qty(x["qty_per_cycle"])) for x in r.get("inputs",  []))
    outs = frozenset((x["item"], _norm_qty(x["qty_per_cycle"])) for x in r.get("outputs", []))
    if with_cycle:
        return (r["machine"], ins, outs, _norm_qty(r.get("cycle_time_s")))
    return (r["machine"], ins, outs)


def _fingerprint_db(machine_name: str, flows: list,
                    cycle_time_s: float | None = None, with_cycle: bool = False) -> tuple:
    """Fingerprint рецепта из БД. Фильтруем только None item (qty=None оставляем)."""
    valid = [f for f in flows if f.get("item") is not None]
    ins  = frozenset((f["item"], _norm_qty(f["qty"])) for f in valid if f["d"] == 0)
    outs = frozenset((f["item"], _norm_qty(f["qty"])) for f in valid if f["d"] == 1)
    if with_cycle:
        return (machine_name, ins, outs, cycle_time_s)
    return (machine_name, ins, outs)


# ── Загрузка данных ──────────────────────────────────────────────

def load_json_recipes(path: Path) -> tuple[dict[tuple, str], dict[tuple, str]]:
    """Возвращает (base_map, cycle_map).
    base_map  — {(machine, ins, outs): wiki_id}
    cycle_map — {(machine, ins, outs, cycle): wiki_id}  (для разрешения конфликтов)
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    base_map: dict[tuple, str]  = {}
    cycle_map: dict[tuple, str] = {}
    base_dups: list[str] = []

    for r in data:
        wiki_id = r.get("recipe_id")
        if not wiki_id:
            continue

        fp       = _fingerprint_json(r, with_cycle=False)
        fp_cycle = _fingerprint_json(r, with_cycle=True)

        if fp in base_map:
            base_dups.append(f"  {wiki_id} vs {base_map[fp]}")
        else:
            base_map[fp] = wiki_id

        # cycle_map: предупреждаем только если и с циклом дубль
        if fp_cycle not in cycle_map:
            cycle_map[fp_cycle] = wiki_id

    if base_dups:
        print(f"[info] Дубли по базовому fingerprint'у ({len(base_dups)}) — попробуем cycle_time_s:")
        for d in base_dups[:5]:
            print(d)

    return base_map, cycle_map


def load_db_recipes(cur) -> list[dict]:
    """Загружает рецепты с потоками из БД."""
    cur.execute("""
        SELECT r.id, r.machine_name, r.cycle_time_s, r.wiki_id, r.machine_id,
               COALESCE(
                   json_agg(
                       json_build_object('d', rf.direction, 'item', i.name, 'qty', rf.qty_per_cycle)
                       ORDER BY rf.direction, i.name
                   ) FILTER (WHERE rf.id IS NOT NULL),
                   '[]'::json
               ) AS flows
        FROM recipes r
        LEFT JOIN resource_flows rf ON rf.recipe_id = r.id AND rf.parent_type = 0
        LEFT JOIN items i ON i.id = rf.item_id
        GROUP BY r.id
        ORDER BY r.id
    """)
    rows = cur.fetchall()
    result = []
    for row in rows:
        flows_raw = row["flows"]
        if flows_raw is None:
            flows = []
        elif isinstance(flows_raw, list):
            flows = flows_raw
        elif isinstance(flows_raw, str):
            flows = json.loads(flows_raw)
        else:
            flows = list(flows_raw)  # psycopg2 may return a list already
        result.append({
            "id":           row["id"],
            "machine_name": row["machine_name"],
            "machine_id":   row["machine_id"],
            "cycle_time_s": float(row["cycle_time_s"]) if row["cycle_time_s"] else None,
            "wiki_id":      row["wiki_id"],
            "flows":        flows,
        })
    return result


# ── Основная логика ──────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    json_path = ROOT / "recipes.json"
    if not json_path.exists():
        print(f"[error] Файл не найден: {json_path}", file=sys.stderr)
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL",
                            "postgresql://postgres:postgres@127.0.0.1:5432/coi_public")
    conn = psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)
    cur  = conn.cursor()

    print("Загружаем recipes.json …")
    json_map, json_cycle_map = load_json_recipes(json_path)
    print(f"  {len(json_map)} уникальных рецептов в JSON")

    print("Загружаем рецепты из БД …")
    db_recipes = load_db_recipes(cur)
    print(f"  {len(db_recipes)} рецептов в БД")

    already_ok  = 0
    to_update   = []   # (db_id, wiki_id)
    unmatched   = []   # db рецепты без совпадения
    conflicted  = []   # wiki_id, на который претендуют >1 db записей

    # Первый проход: базовый fingerprint (machine, ins, outs)
    # Конфликт: один (wiki_id, machine_id) претендует на >1 DB-записей
    wiki_machine_to_db: dict[tuple, list[int]] = {}
    for rec in db_recipes:
        fp = _fingerprint_db(rec["machine_name"], rec["flows"])
        wiki_id = json_map.get(fp)
        if wiki_id:
            key = (wiki_id, rec["machine_id"])
            wiki_machine_to_db.setdefault(key, []).append(rec["id"])

    conflict_keys = {k for k, ids in wiki_machine_to_db.items() if len(ids) > 1}

    for rec in db_recipes:
        fp = _fingerprint_db(rec["machine_name"], rec["flows"])
        wiki_id = json_map.get(fp)

        if wiki_id is None:
            unmatched.append(rec)
            continue

        if (wiki_id, rec["machine_id"]) in conflict_keys:
            # Второй проход: уточняем по cycle_time_s
            fp2 = _fingerprint_db(rec["machine_name"], rec["flows"],
                                   cycle_time_s=rec["cycle_time_s"], with_cycle=True)
            wiki_id2 = json_cycle_map.get(fp2)
            if wiki_id2:
                if rec["wiki_id"] == wiki_id2:
                    already_ok += 1
                else:
                    to_update.append((rec["id"], wiki_id2, rec["machine_name"]))
            else:
                conflicted.append((rec["id"], wiki_id, rec["machine_name"]))
            continue

        if rec["wiki_id"] == wiki_id:
            already_ok += 1
            continue

        to_update.append((rec["id"], wiki_id, rec["machine_name"]))

    # ── Отчёт ───────────────────────────────────────────────────

    print(f"\n{'='*55}")
    print(f"  Уже актуальны (wiki_id совпадает): {already_ok}")
    print(f"  Будет обновлено:                   {len(to_update)}")
    print(f"  Конфликт (пропущены):              {len(conflicted)}")
    print(f"  Не нашли совпадения:               {len(unmatched)}")
    print(f"{'='*55}")

    if conflicted:
        print(f"\n[!] ТРЕБУЕТ ВНИМАНИЯ: {len(conflicted)} рецептов не удалось сопоставить даже по cycle_time_s.")
        print("    Добавьте маппинг вручную через coi_fixes.json или отдельный SQL-скрипт:")
        for db_id, wiki_id, mname in conflicted:
            print(f"  id={db_id:5}  {mname:<30}  предполагаемый wiki_id={wiki_id}")

    if to_update:
        print(f"\nПримеры обновлений (первые 10):")
        for db_id, wiki_id, mname in to_update[:10]:
            print(f"  recipes.id={db_id:5}  {mname:<30}  -> wiki_id={wiki_id}")

    if unmatched:
        print(f"\nНе сопоставлены (первые 20):")
        for rec in unmatched[:20]:
            flows_str = ", ".join(
                f"{'in' if f['d']==0 else 'out'}:{f['item']}x{f['qty']}"
                for f in rec["flows"][:4]
            )
            print(f"  id={rec['id']:5}  {rec['machine_name']:<30}  [{flows_str}]")

    if dry_run:
        print("\n[dry-run] Ничего не изменено.")
        cur.close(); conn.close()
        return

    if not to_update:
        print("\nНечего обновлять.")
        cur.close(); conn.close()
        return

    # ── Применяем ───────────────────────────────────────────────
    print(f"\nПрименяем {len(to_update)} обновлений …")
    cur2 = conn.cursor()
    for db_id, wiki_id, _ in to_update:
        cur2.execute(
            "UPDATE recipes SET wiki_id = %s WHERE id = %s",
            (wiki_id, db_id),
        )
    conn.commit()
    cur2.close(); cur.close(); conn.close()
    print(f"Готово! Обновлено {len(to_update)} записей.")

    if unmatched:
        pct = len(unmatched) / len(db_recipes) * 100
        print(f"\n[warn] {len(unmatched)} рецептов ({pct:.1f}%) остались без wiki_id.")
        print("  Возможные причины:")
        print("  - рецепт удалён из игры (deprecated)")
        print("  - изменился состав ресурсов в новой версии игры")
        print("  - machines.json содержит устаревшее название машины")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill wiki_id in recipes table.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Показать что будет сделано, не применяя изменения.")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
