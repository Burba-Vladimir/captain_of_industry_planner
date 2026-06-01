"""
Наполняет таблицы content_translations, items.po_key, buildings.po_key
из файлов игровой локализации _temp/Translations/{en,ru}.po.

Алгоритм:
  1. Парсим en.po → reverse-map: lowercase(msgstr) → msgid  (только *__name)
  2. Для каждого items.name/buildings.name находим msgid (case-insensitive)
  3. Записываем en + ru переводы в content_translations
  4. Обновляем items.po_key, buildings.po_key

Запуск:
    python populate_translations.py --dry-run   # показать без изменений
    python populate_translations.py             # применить
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
PO_DIR = ROOT / "web_public" / "_temp" / "Translations"


# ── Парсер .po файлов ────────────────────────────────────────────

def parse_po(path: Path) -> dict[str, str]:
    """Возвращает {msgid: msgstr} для всех одиночных msgstr (без plural)."""
    entries: dict[str, str] = {}
    content = path.read_text(encoding="utf-8")
    # Разбиваем на блоки по пустой строке перед следующим msgid
    blocks = re.split(r'\n(?=msgid )', content)
    for block in blocks:
        m_id  = re.search(r'^msgid "(.+)"',  block, re.MULTILINE)
        m_str = re.search(r'^msgstr "(.+)"', block, re.MULTILINE)
        if m_id and m_str:
            entries[m_id.group(1)] = m_str.group(1)
    return entries


def _msgid_priority(msgid: str) -> int:
    """
    Меньше = выше приоритет при конфликте msgstr.
    Правила:
      Research* → низкий приоритет (это узлы исследований, не игровые объекты)
      Crop_*    → ниже Product_* (продукты важнее урожаев для нашего справочника)
    """
    if msgid.startswith("Research"):
        return 10
    if msgid.startswith("Crop_"):
        return 5
    if msgid.startswith("Goal__"):
        return 8
    if msgid.startswith("HealthPoints") or msgid.startswith("WaterNeed"):
        return 9
    return 0


def build_reverse_map(en: dict[str, str]) -> dict[str, str | None]:
    """
    Reverse-map: lowercase(en_msgstr) → msgid
    Только для ключей, оканчивающихся на __name.
    При конфликте выбирается msgid с наименьшим приоритетом (_msgid_priority).
    None = неразрешимая неоднозначность (одинаковый приоритет).
    """
    # best: {lower_msgstr: (msgid, priority)}
    best: dict[str, tuple[str, int]] = {}
    for msgid, msgstr in en.items():
        if not msgid.endswith("__name"):
            continue
        key = msgstr.lower()
        p = _msgid_priority(msgid)
        if key not in best:
            best[key] = (msgid, p)
        else:
            cur_p = best[key][1]
            if p < cur_p:
                best[key] = (msgid, p)
            elif p == cur_p:
                best[key] = (None, p)  # type: ignore  # true ambiguity

    return {k: v[0] for k, v in best.items()}


# ── Основная логика ──────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    po_en_path = PO_DIR / "en.po"
    po_ru_path = PO_DIR / "ru.po"
    for p in (po_en_path, po_ru_path):
        if not p.exists():
            print(f"[error] Файл не найден: {p}", file=sys.stderr)
            sys.exit(1)

    print("Парсим .po файлы …")
    en = parse_po(po_en_path)
    ru = parse_po(po_ru_path)
    rev = build_reverse_map(en)
    print(f"  en.po: {len(en)} записей, {sum(1 for v in rev.values() if v)} уникальных __name")
    print(f"  ru.po: {len(ru)} записей")

    db_url = os.environ.get("DATABASE_URL",
                            "postgresql://postgres:postgres@127.0.0.1:5432/coi_public")
    conn = psycopg2.connect(db_url)
    cur  = conn.cursor()

    # ── Загрузить items и buildings из БД ──────────────────────────
    cur.execute("SELECT id, name FROM items ORDER BY name")
    items = cur.fetchall()
    cur.execute("SELECT id, name FROM buildings ORDER BY name")
    buildings = cur.fetchall()

    # ── Сопоставление ─────────────────────────────────────────────
    stats = {"items_ok": 0, "items_ambig": 0, "items_miss": 0,
             "bld_ok": 0,   "bld_ambig": 0,   "bld_miss": 0}

    item_updates:     list[tuple[str, int]] = []   # (po_key, item_id)
    building_updates: list[tuple[str, int]] = []   # (po_key, building_id)
    translations:     dict[tuple, str]      = {}   # {(po_key, lang): value}
    miss_items: list[str] = []
    miss_bld:   list[str] = []
    ambig_items: list[str] = []
    ambig_bld:   list[str] = []

    def register(en_name: str, entity_id: int, is_building: bool) -> None:
        po_key = rev.get(en_name.lower())
        updates = building_updates if is_building else item_updates
        stat_pfx = "bld" if is_building else "items"
        miss_list = miss_bld if is_building else miss_items
        ambig_list = ambig_bld if is_building else ambig_items

        if po_key is None and en_name.lower() in rev:
            # Присутствует в map, но неоднозначно
            ambig_list.append(en_name)
            stats[f"{stat_pfx}_ambig"] += 1
            return
        if not po_key:
            miss_list.append(en_name)
            stats[f"{stat_pfx}_miss"] += 1
            return

        updates.append((po_key, entity_id))
        stats[f"{stat_pfx}_ok"] += 1

        en_val = en.get(po_key, en_name)
        ru_val = ru.get(po_key)
        translations[(po_key, "en")] = en_val
        if ru_val:
            translations[(po_key, "ru")] = ru_val

    for item_id, name in items:
        register(name, item_id, is_building=False)

    for bld_id, name in buildings:
        register(name, bld_id, is_building=True)

    # ── Отчёт ───────────────────────────────────────────────────
    total_i = len(items)
    total_b = len(buildings)
    print(f"\n{'='*60}")
    print(f"  Items:     {stats['items_ok']}/{total_i} ({stats['items_ok']*100//total_i}%) "
          f"сопоставлено, {stats['items_ambig']} неоднозначных, {stats['items_miss']} нет в .po")
    print(f"  Buildings: {stats['bld_ok']}/{total_b} ({stats['bld_ok']*100//total_b}%) "
          f"сопоставлено, {stats['bld_ambig']} неоднозначных, {stats['bld_miss']} нет в .po")
    print(f"  Переводов: {len(translations)} (en+ru)")
    print(f"{'='*60}")

    if miss_items:
        print(f"\nItems без перевода ({len(miss_items)}):")
        for n in miss_items:
            print(f"  {n}")
    if miss_bld:
        print(f"\nBuildings без перевода ({len(miss_bld)}):")
        for n in miss_bld:
            print(f"  {n}")
    if ambig_items or ambig_bld:
        print(f"\nNеоднозначные (несколько msgid -> один msgstr):")
        for n in ambig_items + ambig_bld:
            # Покажем какие msgid конфликтуют
            candidates = [k for k, v in en.items()
                          if k.endswith("__name") and v.lower() == n.lower()]
            print(f"  {n!r} -> {candidates}")

    if dry_run:
        print("\n[dry-run] Ничего не изменено.")
        cur.close(); conn.close()
        return

    # ── Применяем ─────────────────────────────────────────────────
    print("\nСоздаём таблицы (если нет) …")
    cur.execute("""
        ALTER TABLE items ADD COLUMN IF NOT EXISTS po_key TEXT;
        ALTER TABLE buildings ADD COLUMN IF NOT EXISTS po_key TEXT;
        CREATE TABLE IF NOT EXISTS content_translations (
            po_key TEXT NOT NULL,
            lang   TEXT NOT NULL,
            value  TEXT NOT NULL,
            PRIMARY KEY (po_key, lang)
        );
    """)

    print(f"Обновляем items.po_key ({len(item_updates)}) …")
    for po_key, item_id in item_updates:
        cur.execute("UPDATE items SET po_key = %s WHERE id = %s", (po_key, item_id))

    print(f"Обновляем buildings.po_key ({len(building_updates)}) …")
    for po_key, bld_id in building_updates:
        cur.execute("UPDATE buildings SET po_key = %s WHERE id = %s", (po_key, bld_id))

    print(f"Заполняем content_translations ({len(translations)} записей) …")
    cur.execute("DELETE FROM content_translations")   # полная перезапись
    for (po_key, lang), value in translations.items():
        cur.execute(
            "INSERT INTO content_translations (po_key, lang, value) VALUES (%s, %s, %s)"
            " ON CONFLICT (po_key, lang) DO UPDATE SET value = EXCLUDED.value",
            (po_key, lang, value),
        )

    conn.commit()
    cur.close(); conn.close()
    print("Готово!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate content translations from .po files.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Показать результат без применения изменений.")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
