"""
Captain of Industry — единый парсер данных с wiki.coigame.com

Источники:
  - Рецепты:  Cargo API  /api.php?action=cargoquery&tables=RecipesImport
  - Контракты: HTML страница /Trade  (таблица Contracts)

Результат:
  - recipes.json   — рецепты в формате {machine, recipe_id, cycle_time_s, inputs[], outputs[]}
  - items.json     — отсортированный список всех уникальных предметов
  - contracts.json — контракты с деревнями

Запуск: python coi_parser.py
"""

import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Конфигурация ──────────────────────────────────────────────
BASE_URL   = "https://wiki.coigame.com"
OUT_DIR    = Path(__file__).parent
DELAY      = 0.3
TIMEOUT    = 25
MAX_RETRY  = 3
CARGO_LIMIT = 500   # записей за один запрос (макс. 500 у CargoQuery)

SESSION = requests.Session()
SESSION.trust_env = False   # игнорировать системный прокси
SESSION.headers.update({"User-Agent": "COI-Parser/2.0"})

# Поля рецептов в Cargo-таблице RecipesImport
CARGO_FIELDS = ",".join([
    "RecipeId", "Building", "Time", "PowerMult", "Unreleased",
    *[f"Input{i}Name,Input{i}Qty"  for i in range(1, 7)],
    *[f"Output{i}Name,Output{i}Qty" for i in range(1, 7)],
])


# ── HTTP-помощник ─────────────────────────────────────────────

def get(url: str, params: dict = None) -> requests.Response:
    for attempt in range(MAX_RETRY):
        try:
            r = SESSION.get(url, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as e:
            if attempt == MAX_RETRY - 1:
                raise
            wait = 2 ** attempt
            print(f"    retry {attempt + 1}/{MAX_RETRY} after {wait}s: {e}")
            time.sleep(wait)


# ══════════════════════════════════════════════════════════════
# ЧАСТЬ 1 — РЕЦЕПТЫ через Cargo API
# ══════════════════════════════════════════════════════════════

def fetch_cargo_page(offset: int) -> list[dict]:
    """Одна страница CargoQuery, возвращает список raw-записей."""
    params = {
        "action": "cargoquery",
        "tables":  "RecipesImport",
        "fields":  CARGO_FIELDS,
        "limit":   str(CARGO_LIMIT),
        "offset":  str(offset),
        "format":  "json",
    }
    data = get(f"{BASE_URL}/api.php", params).json()
    return [row["title"] for row in data.get("cargoquery", [])]


def fetch_all_recipes_raw() -> list[dict]:
    """Скачивает все записи RecipesImport с пагинацией."""
    raw, offset = [], 0
    while True:
        page = fetch_cargo_page(offset)
        raw.extend(page)
        print(f"  Cargo offset={offset}: получено {len(page)}, всего {len(raw)}")
        if len(page) < CARGO_LIMIT:
            break
        offset += CARGO_LIMIT
        time.sleep(DELAY)
    return raw


def _num(val, cast=float):
    """Безопасное приведение строки к числу."""
    try:
        return cast(val) if val not in (None, "", "0") else None
    except (ValueError, TypeError):
        return None


def transform_recipes(raw: list[dict]) -> tuple[list[dict], list[str]]:
    """
    Преобразует raw Cargo-записи в финальный формат.
    Возвращает (recipes, sorted_items).
    """
    recipes = []
    items: set[str] = set()

    for row in raw:
        # Пропускаем unreleased-контент
        if row.get("Unreleased") == "1":
            continue

        cycle_time = _num(row.get("Time"), float)

        inputs, outputs = [], []

        for i in range(1, 7):
            name = row.get(f"Input{i}Name") or ""
            qty  = _num(row.get(f"Input{i}Qty"), int)
            if name and qty:
                per_min = round(qty * 60 / cycle_time, 4) if cycle_time else None
                inputs.append({"item": name, "qty_per_cycle": qty, "qty_per_min": per_min})
                items.add(name)


        for i in range(1, 7):
            name = row.get(f"Output{i}Name") or ""
            if not name:
                continue
            qty = _num(row.get(f"Output{i}Qty"), int)
            # qty может быть None/0 если выход вариативный (напр. Waste Sorting Plant)
            per_min = round(qty * 60 / cycle_time, 4) if (qty and cycle_time) else None
            outputs.append({"item": name, "qty_per_cycle": qty, "qty_per_min": per_min})
            items.add(name)

        if not inputs and not outputs:
            continue

        recipe = {
            "recipe_id":    row.get("RecipeId") or None,
            "machine":      row.get("Building") or "",
            "cycle_time_s": cycle_time,
            "inputs":       inputs,
            "outputs":      outputs,
        }
        # PowerMult: множитель мощности (не у всех есть)
        pm = _num(row.get("PowerMult"), float)
        if pm is not None and pm != 1.0:
            recipe["power_mult"] = pm

        recipes.append(recipe)

    return recipes, sorted(items)


# ══════════════════════════════════════════════════════════════
# ЧАСТЬ 2 — КОНТРАКТЫ с HTML /Trade
# ══════════════════════════════════════════════════════════════

def fetch_contracts() -> list[dict]:
    """
    Парсит таблицу Contracts на странице /Trade.

    Структура таблицы (colspan/rowspan):
      Village | RequiredReputation | Export (item, qty) | Import (item, qty)
      | Unity (per_month, per_ship, at_establish)

    Первые две строки — заголовки (rowspan=2 и sub-headers).
    Строки с Village='-' или Export='Nothing' — пустые слоты, пропускаем.
    """
    r = get(f"{BASE_URL}/Trade")
    soup = BeautifulSoup(r.text, "html.parser")

    # Contracts — вторая таблица на странице
    tables = soup.find_all("table")
    if len(tables) < 2:
        raise RuntimeError("Таблица контрактов не найдена")

    table = tables[1]
    contracts = []

    # Пропускаем первые 2 строки (двойной заголовок)
    for row in table.find_all("tr")[2:]:
        cells = [td.get_text(" ", strip=True) for td in row.find_all("td")]
        if len(cells) < 9:
            continue

        village_raw  = cells[0]
        rep_raw      = cells[1]
        export_item  = cells[2]
        export_qty   = cells[3]
        import_item  = cells[4]
        import_qty   = cells[5]
        unity_month  = cells[6]
        unity_ship   = cells[7]
        unity_est    = cells[8]

        # Пропускаем пустые / Nothing строки
        if export_item in ("Nothing", "-", "") and import_item in ("Nothing", "-", ""):
            continue

        def safe_int(v):
            try:    return int(v.replace(",", "").replace(" ", ""))
            except: return None

        def safe_float(v):
            try:    return float(v.replace(",", ".").replace(" ", ""))
            except: return None

        contract = {
            "village":              safe_int(village_raw),
            "required_reputation":  safe_int(rep_raw),
            "export_item":          export_item  if export_item  not in ("-", "") else None,
            "export_qty":           safe_int(export_qty),
            "import_item":          import_item  if import_item  not in ("-", "") else None,
            "import_qty":           safe_int(import_qty),
            "unity_per_month":      safe_float(unity_month),
            "unity_per_ship":       safe_float(unity_ship),
            "unity_at_establish":   safe_float(unity_est),
        }
        contracts.append(contract)

    return contracts


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════

def save(data, path: Path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  -> {path.name}  ({len(data)} записей)")


def main():
    print("=" * 55)
    print("Captain of Industry — парсер v2")
    print("=" * 55)

    # --- Рецепты ---
    print("\n[1/2] Рецепты (Cargo API)...")
    raw = fetch_all_recipes_raw()
    print(f"  Сырых записей: {len(raw)}")

    recipes, items = transform_recipes(raw)
    print(f"  После фильтрации: {len(recipes)} рецептов, {len(items)} предметов")

    save(recipes, OUT_DIR / "recipes.json")
    save(items,   OUT_DIR / "items.json")

    # --- Контракты ---
    print("\n[2/2] Контракты (Trade page)...")
    contracts = fetch_contracts()
    print(f"  Контрактов: {len(contracts)}")
    save(contracts, OUT_DIR / "contracts.json")

    print("\nGotovo!")


if __name__ == "__main__":
    main()
