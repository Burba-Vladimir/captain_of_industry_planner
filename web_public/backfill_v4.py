#!/usr/bin/env python3
"""
backfill_v4.py — Populate buildings.computing_tf and recipes.power_multiplier
from the captain-of-data JSON (github.com/David-Melo/captain-of-data).

Usage:
    python backfill_v4.py              # apply changes
    python backfill_v4.py --dry-run    # preview only
    python backfill_v4.py --db-url postgresql://user:pass@host/dbname

Requires: psycopg2-binary (pip install psycopg2-binary)
DATABASE_URL env var is used if --db-url is not provided.
"""

import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 not found. Install with:  pip install psycopg2-binary")
    sys.exit(1)

DATA_URL = (
    "https://raw.githubusercontent.com/David-Melo/captain-of-data/"
    "main/data/machines_and_buildings.json"
)


# ── helpers ────────────────────────────────────────────────────────────────────

def download_json(url: str) -> dict:
    print(f"Downloading {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "backfill_v4/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    print(f"  game_version: {data.get('game_version', 'unknown')}")
    return data


def _input_items_from_recipe(jr: dict) -> frozenset[str]:
    """Extract input item names from a JSON recipe object (tries several key shapes)."""
    # Shape 1: separate "inputs" list with {"name": ...}
    if "inputs" in jr:
        items = [
            e.get("name") or e.get("product") or e.get("item", "")
            for e in jr["inputs"]
            if isinstance(e, dict)
        ]
        if any(items):
            return frozenset(filter(None, items))

    # Shape 2: unified "products" list with a "type" discriminator
    if "products" in jr:
        items = [
            e.get("name") or e.get("product") or e.get("item", "")
            for e in jr["products"]
            if isinstance(e, dict) and e.get("type", "").lower() in ("input", "ingredient", "")
        ]
        if any(items):
            return frozenset(filter(None, items))

    # Shape 3: "input_products"
    if "input_products" in jr:
        items = [
            e.get("name") or e.get("product") or e.get("item", "")
            for e in jr["input_products"]
            if isinstance(e, dict)
        ]
        return frozenset(filter(None, items))

    return frozenset()


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print planned changes without writing to DB")
    parser.add_argument("--db-url", default=None,
                        help="PostgreSQL connection string (overrides DATABASE_URL env)")
    args = parser.parse_args()

    dry_run = args.dry_run
    db_url = args.db_url or os.environ.get("DATABASE_URL") or "dbname=coi_planner"

    # ── Download captain-of-data ──────────────────────────────────────────────
    raw = download_json(DATA_URL)
    machines: list[dict] = raw.get("machines_and_buildings", raw)
    if not isinstance(machines, list):
        print(f"ERROR: Expected list for machines_and_buildings, got {type(machines).__name__}")
        sys.exit(1)
    print(f"  {len(machines)} machine/building entries loaded\n")

    # ── Connect ───────────────────────────────────────────────────────────────
    con = psycopg2.connect(db_url)
    cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # ══ Fix 1: computing_tf ═══════════════════════════════════════════════════
    print("── Fix 1: computing_tf ─────────────────────────────────────────────")
    cur.execute("SELECT id, name FROM buildings")
    db_buildings: dict[str, int] = {row["name"]: row["id"] for row in cur.fetchall()}

    computing_updates: list[tuple[float, int]] = []
    for m in machines:
        name = m.get("name", "")
        consumed  = float(m.get("computing_consumed",  0) or 0)
        generated = float(m.get("computing_generated", 0) or 0)
        net_tf = generated - consumed
        if net_tf == 0.0:
            continue
        if name not in db_buildings:
            print(f"  SKIP  (not in DB): {name!r}  net_tf={net_tf:+g}")
            continue
        computing_updates.append((net_tf, db_buildings[name]))
        sign = "+" if net_tf > 0 else ""
        print(f"  UPDATE buildings: {name!r}  computing_tf = {sign}{net_tf:g} TF")

    print(f"\n  → {len(computing_updates)} building(s) to update\n")

    if not dry_run and computing_updates:
        cur.executemany(
            "UPDATE buildings SET computing_tf = %s WHERE id = %s",
            computing_updates,
        )

    # ══ Fix 3: power_multiplier ═══════════════════════════════════════════════
    print("── Fix 3: power_multiplier ─────────────────────────────────────────")

    # Fetch all DB recipes with their input items for matching
    cur.execute("""
        SELECT r.id AS recipe_id, r.machine_name,
               COALESCE(
                   json_agg(i.name ORDER BY rf.sort_order)
                   FILTER (WHERE rf.direction = 0 AND i.name IS NOT NULL),
                   '[]'::json
               ) AS input_items
        FROM   recipes r
        LEFT   JOIN resource_flows rf ON rf.recipe_id  = r.id
                                      AND rf.parent_type = 0
        LEFT   JOIN items i ON i.id = rf.item_id
        GROUP  BY r.id, r.machine_name
    """)
    db_recipe_rows = cur.fetchall()

    # Build lookup:  machine_name → list of (recipe_id, frozenset[item_name])
    machine_recipes: dict[str, list[tuple[int, frozenset[str]]]] = defaultdict(list)
    for row in db_recipe_rows:
        raw_inputs = row["input_items"]
        if isinstance(raw_inputs, str):
            raw_inputs = json.loads(raw_inputs)
        item_set: frozenset[str] = frozenset(filter(None, raw_inputs or []))
        machine_recipes[row["machine_name"]].append((row["recipe_id"], item_set))

    power_updates: list[tuple[float, int]] = []
    unmatched: list[tuple[str, frozenset, float]] = []

    for m in machines:
        machine_name: str = m.get("name", "")
        json_recipes: list[dict] = m.get("recipes", [])
        if not json_recipes:
            continue

        for jr in json_recipes:
            pm_raw = jr.get("power_multiplier")
            if pm_raw is None:
                continue
            pm = float(pm_raw)
            if pm == 1.0:
                continue

            json_inputs = _input_items_from_recipe(jr)
            candidates  = machine_recipes.get(machine_name, [])

            matched_id: int | None = None
            for recipe_id, db_inputs in candidates:
                # Exact match or json inputs ⊆ db inputs (accounts for
                # captain-of-data omitting some secondary inputs)
                if json_inputs == db_inputs or (json_inputs and json_inputs.issubset(db_inputs)):
                    matched_id = recipe_id
                    break

            if matched_id is not None:
                power_updates.append((pm, matched_id))
                print(
                    f"  UPDATE recipes: {machine_name!r}  "
                    f"inputs={sorted(json_inputs)!r}  power_multiplier = {pm}"
                )
            else:
                unmatched.append((machine_name, json_inputs, pm))

    print(f"\n  → {len(power_updates)} recipe(s) to update")
    if unmatched:
        print(f"  → {len(unmatched)} unmatched recipe(s):")
        for mn, inp, pm in unmatched:
            print(f"      {mn!r}  inputs={sorted(inp)!r}  pm={pm}")
    print()

    if not dry_run and power_updates:
        cur.executemany(
            "UPDATE recipes SET power_multiplier = %s WHERE id = %s",
            power_updates,
        )

    # ── Commit ────────────────────────────────────────────────────────────────
    if dry_run:
        print("DRY RUN — no changes written to database.")
    else:
        con.commit()
        print("✓ Changes committed to database.")

    cur.close()
    con.close()


if __name__ == "__main__":
    main()
