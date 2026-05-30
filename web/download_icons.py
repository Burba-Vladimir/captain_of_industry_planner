"""
Скачивает иконки предметов с вики Captain of Industry.

Запуск:
    cd web
    python download_icons.py [--recheck]

  --recheck  проверить уже скачанные файлы и перекачать повреждённые

Файлы сохраняются в  web/static/icons/<Item_Name>.png
"""
from __future__ import annotations

import argparse
import os
import struct
import time
import zlib

import requests
import psycopg2

# ── Настройки БД ────────────────────────────────────────────────
DB = {
    "host":     "127.0.0.1",
    "port":     5432,
    "dbname":   "capitan_of_industry",
    "user":     "postgres",
    "password": "postgres",
}

ICONS_DIR   = os.path.join(os.path.dirname(__file__), "static", "icons")
WIKI_API    = "https://wiki.coigame.com/api.php"
BATCH_SIZE  = 50
DELAY_S     = 0.3

PNG_SIG   = b"\x89PNG\r\n\x1a\n"   # Первые 8 байт PNG-файла
GIF_SIGS  = (b"GIF87a", b"GIF89a") # GIF-сигнатуры

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CaptainOfIndustry-icons/1.1 (local)"})
SESSION.trust_env = False


# ─────────────────────────────────────────────────────────────────
# Проверка файлов
# ─────────────────────────────────────────────────────────────────

def is_valid_png(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(8) == PNG_SIG
    except OSError:
        return False

def is_valid_gif(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            hdr = f.read(6)
            return hdr in GIF_SIGS
    except OSError:
        return False

def file_ok(path: str) -> bool:
    """True если файл существует и начинается корректной сигнатурой."""
    if not os.path.exists(path):
        return False
    return is_valid_png(path) or is_valid_gif(path)


# ─────────────────────────────────────────────────────────────────
# Данные из БД
# ─────────────────────────────────────────────────────────────────

def get_item_names() -> list[str]:
    con = psycopg2.connect(**DB)
    try:
        with con.cursor() as cur:
            cur.execute("SELECT name FROM items ORDER BY name")
            names = [r[0] for r in cur.fetchall()]
            # Типы обслуживания не в таблице items, добавляем отдельно
            cur.execute("SELECT DISTINCT item FROM building_maintenance ORDER BY item")
            maint = [r[0] for r in cur.fetchall()]
            return sorted(set(names) | set(maint))
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────────
# MediaWiki API — получить прямые URL изображений
# ─────────────────────────────────────────────────────────────────

def get_image_urls(names: list[str]) -> dict[str, str]:
    """
    Возвращает {item_name: direct_image_url}.
    Отсутствующие файлы в результат не попадают.
    """
    result: dict[str, str] = {}

    # Маппинг нормализованного заголовка → имя предмета
    title_to_name: dict[str, str] = {}
    file_titles: list[str] = []
    for name in names:
        # пробуем без расширения и с .png
        base = name.replace(" ", "_")
        ft   = "File:" + base + ".png"
        file_titles.append(ft)
        # несколько вариантов ключа на случай нормализации wiki
        title_to_name[ft.lower()] = name
        title_to_name[("File:" + base).lower()] = name
        title_to_name[base.lower()] = name

    for i in range(0, len(file_titles), BATCH_SIZE):
        batch = file_titles[i : i + BATCH_SIZE]
        params = {
            "action":   "query",
            "titles":   "|".join(batch),
            "prop":     "imageinfo",
            "iiprop":   "url|mime",   # запросить MIME для проверки формата
            "format":   "json",
        }
        try:
            resp = SESSION.get(WIKI_API, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  [!] API ошибка батч {i}: {e}")
            time.sleep(DELAY_S * 5)
            continue

        # Нормализованные заголовки (wiki может переименовывать пробелы→_)
        normalized = {
            v.lower(): n.lower()
            for v, n in (
                (x.get("to", ""), x.get("from", ""))
                for x in data.get("query", {}).get("normalized", [])
            )
        }

        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            if page.get("ns") != 6:
                continue
            imageinfo = page.get("imageinfo", [])
            if not imageinfo:
                continue
            info  = imageinfo[0]
            url   = info.get("url", "")
            mime  = info.get("mime", "")
            if not url:
                continue

            # Найти исходное имя предмета
            title = page.get("title", "").lower()
            name  = (
                title_to_name.get(title) or
                title_to_name.get(normalized.get(title, "")) or
                title_to_name.get(title.replace(" ", "_"))
            )
            if not name:
                continue

            result[name] = (url, mime)

        done = i + len(batch)
        print(f"  API: обработано {done}/{len(file_titles)}, URL найдено: {len(result)}")
        time.sleep(DELAY_S)

    return result          # {name: (url, mime)}


# ─────────────────────────────────────────────────────────────────
# Скачивание
# ─────────────────────────────────────────────────────────────────

def download_icons(url_map: dict[str, tuple[str, str]],
                   recheck: bool = False) -> None:
    os.makedirs(ICONS_DIR, exist_ok=True)

    done = skipped = failed = corrupt = 0
    total = len(url_map)

    for name, (url, mime) in url_map.items():
        # Определяем расширение из MIME
        if "gif" in mime:
            ext = ".gif"
        elif "svg" in mime:
            # SVG не поддерживается как <img> в старых браузерах, но обычно работает
            ext = ".svg"
        else:
            ext = ".png"   # jpeg/png/webp — сохраняем как .png (браузер разберётся)

        # Путь для хранения — всегда item_name.png (для URL)
        path_png = os.path.join(ICONS_DIR, name.replace(" ", "_") + ".png")

        if not recheck and file_ok(path_png):
            skipped += 1
            continue

        if recheck and file_ok(path_png):
            skipped += 1
            continue

        # Скачиваем
        try:
            r = SESSION.get(url, timeout=20)
            r.raise_for_status()

            # Проверяем Content-Type
            ct = r.headers.get("Content-Type", "")
            if "text/html" in ct or "application/xhtml" in ct:
                print(f"  ✗ {name}: сервер вернул HTML (редирект/ошибка)")
                failed += 1
                continue

            data = r.content
            if len(data) < 16:
                print(f"  ✗ {name}: файл слишком мал ({len(data)} байт)")
                failed += 1
                continue

            # Сигнатурная проверка
            if not (data[:8] == PNG_SIG or data[:6] in GIF_SIGS or
                    data[:4] in (b"RIFF",) or    # WebP
                    data[:2] in (b"\xff\xd8",)):  # JPEG
                print(f"  ✗ {name}: неизвестный формат (первые байты: {data[:8].hex()})")
                failed += 1
                continue

            with open(path_png, "wb") as f:
                f.write(data)

            done += 1
            if done <= 20 or done % 50 == 0:
                print(f"  ✓ {name.replace(' ','_')}.png  ({len(data)//1024 or 1} КБ)")

        except Exception as e:
            failed += 1
            print(f"  ✗ {name}: {e}")

        time.sleep(0.1)

    print(f"\nИтого: скачано {done}, пропущено {skipped}, "
          f"ошибок {failed} / {total}")


# ─────────────────────────────────────────────────────────────────
# Диагностика уже скачанных файлов
# ─────────────────────────────────────────────────────────────────

def diagnose() -> None:
    """Проверяет все файлы в static/icons/ и выводит отчёт."""
    if not os.path.exists(ICONS_DIR):
        print("Папка static/icons/ не найдена.")
        return

    files = [f for f in os.listdir(ICONS_DIR) if f.endswith(".png")]
    print(f"Всего файлов: {len(files)}")

    bad: list[str] = []
    small: list[str] = []
    ok: int = 0

    for fname in sorted(files):
        path = os.path.join(ICONS_DIR, fname)
        size = os.path.getsize(path)
        if size < 100:
            small.append(f"  {fname}  ({size} байт)")
            bad.append(fname)
        elif not file_ok(path):
            try:
                with open(path, "rb") as f:
                    hdr = f.read(16).hex()
            except Exception:
                hdr = "?"
            bad.append(fname)
            print(f"  ✗ {fname}  ({size} байт) — не PNG/GIF. Начало: {hdr}")
        else:
            ok += 1

    if small:
        print(f"\nСлишком маленькие файлы ({len(small)}):")
        for s in small:
            print(s)

    print(f"\nОК: {ok},  Повреждено/неверный формат: {len(bad)}")

    if bad:
        print("\nДля перекачки повреждённых файлов удалите их и запустите скрипт снова:")
        for f in bad[:10]:
            print(f"  del static\\icons\\{f}")
        if len(bad) > 10:
            print(f"  … и ещё {len(bad)-10}")


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnose", action="store_true",
                        help="только проверить существующие файлы, не скачивать")
    parser.add_argument("--recheck",  action="store_true",
                        help="перекачать повреждённые файлы")
    args = parser.parse_args()

    if args.diagnose:
        print("=== Диагностика иконок ===")
        diagnose()
    else:
        print("1. Загружаем имена предметов из БД…")
        names = get_item_names()
        print(f"   Найдено предметов: {len(names)}")

        print("\n2. Запрашиваем URL изображений у вики…")
        url_map = get_image_urls(names)
        print(f"   URL найдено: {len(url_map)} из {len(names)}")

        not_found = set(names) - set(url_map)
        if not_found:
            print(f"   Без иконки ({len(not_found)}): "
                  + ", ".join(sorted(not_found)[:15])
                  + ("…" if len(not_found) > 15 else ""))

        print(f"\n3. Скачиваем в {ICONS_DIR} …")
        download_icons(url_map, recheck=args.recheck)

        print("\n4. Проверяем скачанные файлы…")
        diagnose()
