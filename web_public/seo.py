"""
SEO-слой: индексируемые страницы справочника и служебные файлы для поисковиков.

Основное приложение — SPA: без JS краулер видит пустую страницу. Этот модуль
отдаёт серверный HTML по игровым данным, которые и так лежат в БД:

    /items                каталог ресурсов
    /item/<slug>          ресурс: чем производится, где потребляется
    /buildings            каталог зданий
    /building/<slug>      здание: характеристики и все его рецепты
    /robots.txt
    /sitemap.xml

Слаги строятся из английских имён и стабильны при смене языка интерфейса.
"""
from __future__ import annotations

import os
import re
import threading

from flask import (Blueprint, Response, abort, g, redirect, render_template,
                   request, url_for)

from db import dict_cursor, get_db

seo_bp = Blueprint("seo", __name__)

# Языки, для которых генерируются alternate-ссылки
SEO_LANGS = ("en", "ru")


# ─────────────────────────────────────────────────────────────────
# Слаги
# ─────────────────────────────────────────────────────────────────

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """'Steam (High)' → 'steam-high'. Только английские имена из БД."""
    return _SLUG_STRIP.sub("-", (name or "").lower()).strip("-")


# Индексы slug↔id строятся один раз: игровые данные не меняются во время работы.
_idx_lock = threading.Lock()
_idx_cache: dict[str, dict] = {}


def _build_indexes() -> dict:
    """{'item_by_slug': {slug: (id, name)}, 'item_slug': {id: slug}, ...}

    В БД есть дубли-легаси ресурсов без единого рецепта (BlanketFuel при живом
    Blanket Fuel). Страниц у них нет, поэтому слаги раздаём сначала «живым»
    ресурсам — иначе дубль занял бы канонический слаг и увёл ссылку в 404.
    """
    items: dict[str, tuple[int, str]] = {}
    item_slug: dict[int, str] = {}
    builds: dict[str, tuple[int, str]] = {}
    build_slug: dict[int, str] = {}

    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("""
                SELECT i.id, i.name, EXISTS (
                           SELECT 1 FROM resource_flows rf
                           WHERE rf.item_id = i.id AND rf.parent_type = 0
                       ) AS has_recipes
                FROM items i
                ORDER BY has_recipes DESC, i.id
            """)
            rows_i = cur.fetchall()
            cur.execute("SELECT id, name, TRUE FROM buildings ORDER BY id")
            rows_b = cur.fetchall()

    def fill(rows, by_slug, by_id):
        live = set()
        for _id, name, ok in rows:
            base = slugify(name) or f"n{_id}"
            slug = base
            n = 2
            while slug in by_slug:          # коллизия слагов — добавляем суффикс
                slug = f"{base}-{n}"
                n += 1
            by_slug[slug] = (_id, name)
            by_id[_id] = slug
            if ok:
                live.add(_id)
        return live

    live_items = fill(rows_i, items, item_slug)
    fill(rows_b, builds, build_slug)
    return {
        "item_by_slug":     items,
        "item_slug":        item_slug,
        "item_live":        live_items,
        "building_by_slug": builds,
        "building_slug":    build_slug,
    }


def indexes() -> dict:
    global _idx_cache
    if _idx_cache:
        return _idx_cache
    with _idx_lock:
        if not _idx_cache:
            _idx_cache = _build_indexes()
    return _idx_cache


def item_url(name: str) -> str | None:
    """URL страницы ресурса по английскому имени (для ссылок внутри таблиц).

    None для ресурсов без рецептов — у них нет страницы, ссылка вела бы в 404.
    """
    idx = indexes()
    entry = idx["item_by_slug"].get(slugify(name))
    if entry and entry[0] in idx["item_live"]:
        return url_for("seo.item_page", slug=idx["item_slug"][entry[0]])
    return None


def building_url(name: str) -> str | None:
    idx = indexes()
    slug = slugify(name)
    if slug in idx["building_by_slug"]:
        return url_for("seo.building_page", slug=slug)
    return None


# ─────────────────────────────────────────────────────────────────
# Локализация игровых имён
# ─────────────────────────────────────────────────────────────────

def loc(name: str) -> str:
    """Английское имя → локализованное (по g.content_trans, как в остальном app)."""
    return (getattr(g, "content_trans", None) or {}).get(name, name)


def icon_file(name: str) -> str:
    """Имя PNG-иконки ресурса: пробелы → подчёркивания (как в static/icons)."""
    return (name or "").replace(" ", "_") + ".png"


# ─────────────────────────────────────────────────────────────────
# Запросы
# ─────────────────────────────────────────────────────────────────

# Подзапросы входов/выходов рецепта — те же, что в _make_nodes_sql
_FLOW_LATERAL = """
    LEFT JOIN LATERAL (
        SELECT json_agg(json_build_object('item', i.name,
                                          'qty_per_cycle', rf2.qty_per_cycle,
                                          'qty_per_min',   rf2.qty_per_min)
                        ORDER BY rf2.sort_order) AS items
        FROM resource_flows rf2 JOIN items i ON i.id = rf2.item_id
        WHERE rf2.parent_type = 0 AND rf2.recipe_id = r.id AND rf2.direction = {d}
    ) {alias} ON TRUE
"""

_RECIPE_COLS = """
    r.id                AS recipe_id,
    r.machine_name,
    r.cycle_time_s,
    r.deprecated,
    b.workers,
    b.electricity_kw * COALESCE(r.power_multiplier, 1.0) AS electricity_kw,
    b.computing_tf,
    inp.items           AS inputs,
    outp.items          AS outputs
"""


def _recipes_touching_item(item_id: int, direction: int) -> list[dict]:
    """direction: 0 — рецепты, потребляющие ресурс; 1 — производящие."""
    sql = f"""
        SELECT {_RECIPE_COLS},
               rf.qty_per_cycle AS focus_qty_per_cycle,
               rf.qty_per_min   AS focus_qty_per_min
        FROM resource_flows rf
        JOIN recipes r    ON r.id = rf.recipe_id
        LEFT JOIN buildings b ON b.id = r.machine_id
        {_FLOW_LATERAL.format(d=0, alias='inp')}
        {_FLOW_LATERAL.format(d=1, alias='outp')}
        WHERE rf.parent_type = 0 AND rf.item_id = %s AND rf.direction = %s
        ORDER BY r.deprecated, r.machine_name, r.id
    """
    with get_db() as con:
        with dict_cursor(con) as cur:
            cur.execute(sql, (item_id, direction))
            return [dict(r) for r in cur.fetchall()]


def _recipes_of_building(building_id: int) -> list[dict]:
    sql = f"""
        SELECT {_RECIPE_COLS}
        FROM recipes r
        LEFT JOIN buildings b ON b.id = r.machine_id
        {_FLOW_LATERAL.format(d=0, alias='inp')}
        {_FLOW_LATERAL.format(d=1, alias='outp')}
        WHERE r.machine_id = %s
        ORDER BY r.deprecated, r.id
    """
    with get_db() as con:
        with dict_cursor(con) as cur:
            cur.execute(sql, (building_id,))
            return [dict(r) for r in cur.fetchall()]


def _building_row(building_id: int) -> dict | None:
    with get_db() as con:
        with dict_cursor(con) as cur:
            cur.execute("""
                SELECT b.id, b.name, b.workers, b.electricity_kw, b.computing_tf,
                       b.footprint, b.designation,
                       mnt.items AS maintenance, con_.items AS construction
                FROM buildings b
                LEFT JOIN LATERAL (
                    SELECT json_agg(json_build_object('item', bm.item,
                                                      'rate_per_min', bm.rate_per_min)
                                    ORDER BY bm.item) AS items
                    FROM building_maintenance bm WHERE bm.building_id = b.id
                ) mnt ON TRUE
                LEFT JOIN LATERAL (
                    SELECT json_agg(json_build_object('item', bc.item, 'qty', bc.qty)
                                    ORDER BY bc.item) AS items
                    FROM building_construction bc WHERE bc.building_id = b.id
                ) con_ ON TRUE
                WHERE b.id = %s
            """, (building_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def _items_catalog() -> list[dict]:
    """Все ресурсы, у которых есть хотя бы один рецепт (осиротевшие не индексируем)."""
    with get_db() as con:
        with dict_cursor(con) as cur:
            cur.execute("""
                SELECT i.id, i.name,
                       COUNT(*) FILTER (WHERE rf.direction = 1) AS made_by,
                       COUNT(*) FILTER (WHERE rf.direction = 0) AS used_by
                FROM items i
                JOIN resource_flows rf
                  ON rf.item_id = i.id AND rf.parent_type = 0
                GROUP BY i.id, i.name
                ORDER BY i.name
            """)
            return [dict(r) for r in cur.fetchall()]


def _buildings_catalog() -> list[dict]:
    with get_db() as con:
        with dict_cursor(con) as cur:
            cur.execute("""
                SELECT b.id, b.name, b.workers, b.electricity_kw, b.designation,
                       COUNT(r.id) AS recipe_count
                FROM buildings b
                LEFT JOIN recipes r ON r.machine_id = b.id
                GROUP BY b.id, b.name, b.workers, b.electricity_kw, b.designation
                ORDER BY b.name
            """)
            return [dict(r) for r in cur.fetchall()]


# ─────────────────────────────────────────────────────────────────
# Абсолютные URL
# ─────────────────────────────────────────────────────────────────

def site_url(path: str = "/") -> str:
    """Абсолютный URL. SITE_URL из окружения важен за прокси, где host_url без https."""
    base = (os.environ.get("SITE_URL") or request.host_url).rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _alternates(path: str) -> list[dict]:
    """hreflang-варианты страницы: один URL на язык + x-default."""
    out = [{"lang": lg, "url": site_url(path + "?lang=" + lg)} for lg in SEO_LANGS]
    out.append({"lang": "x-default", "url": site_url(path)})
    return out


# ─────────────────────────────────────────────────────────────────
# Страницы справочника
# ─────────────────────────────────────────────────────────────────

@seo_bp.route("/items")
def items_catalog():
    rows = _items_catalog()
    idx = indexes()
    for r in rows:
        r["slug"] = idx["item_slug"].get(r["id"])
        r["display"] = loc(r["name"])
        r["icon"] = icon_file(r["name"])
    rows.sort(key=lambda r: r["display"].casefold())
    return render_template("seo_catalog.html",
                           kind="items",
                           rows=rows,
                           canonical=site_url("/items"),
                           alternates=_alternates("/items"),
                           user=g.get("user"))


@seo_bp.route("/buildings")
def buildings_catalog():
    rows = _buildings_catalog()
    idx = indexes()
    for r in rows:
        r["slug"] = idx["building_slug"].get(r["id"])
        r["display"] = loc(r["name"])
        r["icon"] = icon_file(r["name"])
    rows.sort(key=lambda r: r["display"].casefold())
    return render_template("seo_catalog.html",
                           kind="buildings",
                           rows=rows,
                           canonical=site_url("/buildings"),
                           alternates=_alternates("/buildings"),
                           user=g.get("user"))


@seo_bp.route("/item/<slug>")
def item_page(slug: str):
    idx = indexes()
    entry = idx["item_by_slug"].get(slug.lower())
    if not entry:
        abort(404)
    item_id, name_en = entry

    produced = _recipes_touching_item(item_id, 1)
    consumed = _recipes_touching_item(item_id, 0)
    if not produced and not consumed:
        abort(404)

    path = url_for("seo.item_page", slug=slug)
    return render_template("seo_item.html",
                           name_en=name_en,
                           display=loc(name_en),
                           icon=icon_file(name_en),
                           produced=produced,
                           consumed=consumed,
                           canonical=site_url(path),
                           alternates=_alternates(path),
                           user=g.get("user"))


@seo_bp.route("/building/<slug>")
def building_page(slug: str):
    idx = indexes()
    entry = idx["building_by_slug"].get(slug.lower())
    if not entry:
        abort(404)
    building_id, name_en = entry

    row = _building_row(building_id)
    if not row:
        abort(404)
    recipes = _recipes_of_building(building_id)

    path = url_for("seo.building_page", slug=slug)
    return render_template("seo_building.html",
                           b=row,
                           name_en=name_en,
                           display=loc(name_en),
                           icon=icon_file(name_en),
                           recipes=recipes,
                           canonical=site_url(path),
                           alternates=_alternates(path),
                           user=g.get("user"))


# ─────────────────────────────────────────────────────────────────
# robots.txt / sitemap.xml
# ─────────────────────────────────────────────────────────────────

# Служебные разделы, которые не нужны в индексе
_DISALLOW = [
    "/api/",
    "/auth/",
    "/complex/new",
    "/complex/*/edit",
    "/set-lang/",
    "/set-theme/",
]


@seo_bp.route("/robots.txt")
def robots():
    lines = ["User-agent: *", "Allow: /"]
    lines += ["Disallow: " + p for p in _DISALLOW]
    lines += ["", "Sitemap: " + site_url("/sitemap.xml"), ""]
    return Response("\n".join(lines), mimetype="text/plain")


@seo_bp.route("/sitemap.xml")
def sitemap():
    idx = indexes()
    urls: list[dict] = [
        {"loc": site_url("/"),           "priority": "1.0", "changefreq": "weekly"},
        {"loc": site_url("/items"),      "priority": "0.8", "changefreq": "monthly"},
        {"loc": site_url("/buildings"),  "priority": "0.8", "changefreq": "monthly"},
        {"loc": site_url("/population"), "priority": "0.7", "changefreq": "monthly"},
        {"loc": site_url("/about"),      "priority": "0.5", "changefreq": "monthly"},
    ]

    # Только ресурсы с рецептами: остальные отдают 404, им в sitemap не место
    for row in _items_catalog():
        slug = idx["item_slug"].get(row["id"])
        if slug:
            urls.append({"loc": site_url("/item/" + slug),
                         "priority": "0.7", "changefreq": "monthly"})
    for _id, slug in sorted(idx["building_slug"].items()):
        urls.append({"loc": site_url("/building/" + slug),
                     "priority": "0.6", "changefreq": "monthly"})

    # Публичные комплексы сообщества
    try:
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT slug, updated_at
                    FROM complexes
                    WHERE visibility = 'public' AND slug IS NOT NULL
                    ORDER BY updated_at DESC
                    LIMIT 5000
                """)
                for cslug, updated in cur.fetchall():
                    urls.append({
                        "loc": site_url("/complex/" + str(cslug) + "/view"),
                        "lastmod": updated.date().isoformat() if updated else None,
                        "priority": "0.6", "changefreq": "weekly",
                    })
    except Exception:
        pass    # sitemap не должен падать из-за раздела комплексов

    xml = render_template("sitemap.xml", urls=urls, langs=SEO_LANGS)
    return Response(xml, mimetype="application/xml")


# ─────────────────────────────────────────────────────────────────
# Счётчики контента для лендинга
# ─────────────────────────────────────────────────────────────────

_counts_cache: dict[str, int] = {}


def content_counts() -> dict[str, int]:
    """{'recipes': n, 'buildings': n, 'items': n} — для текста на главной.

    Числа в лендинге должны быть настоящими: считаем из БД один раз за процесс.
    """
    global _counts_cache
    if _counts_cache:
        return _counts_cache
    try:
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT (SELECT COUNT(*) FROM recipes),
                           (SELECT COUNT(*) FROM buildings),
                           (SELECT COUNT(DISTINCT rf.item_id) FROM resource_flows rf
                            WHERE rf.parent_type = 0)
                """)
                r, b, i = cur.fetchone()
        _counts_cache = {"recipes": r, "buildings": b, "items": i}
    except Exception:
        return {"recipes": 0, "buildings": 0, "items": 0}
    return _counts_cache
