"""
Captain of Industry — публичная веб-версия
────────────────────────────────────────────────────────────────────
Установка:  pip install -r requirements.txt
Настройка:  скопировать .env.example → .env и заполнить
Запуск:     python app.py
Браузер:    http://localhost:5001
"""
from __future__ import annotations

import decimal
import json
import os
import re

from dotenv import load_dotenv
load_dotenv()  # загрузить .env до инициализации Flask


# ─────────────────────────────────────────────────────────────────
# Парсер поисковых запросов
# Синтаксис: in:ресурс  out:ресурс  name:машина  & | ()
# Без спецсимволов — legacy-поиск по всем полям.
# ─────────────────────────────────────────────────────────────────

def _tokenize_query(q: str) -> list:
    """Разбивает строку запроса на токены."""
    tokens = []
    i = 0
    while i < len(q):
        c = q[i]
        if c in ' \t':
            i += 1
            continue
        if c in '&|()':
            tokens.append(c)
            i += 1
            continue
        if c == '"':
            end = q.find('"', i + 1)
            if end == -1:
                end = len(q)
            tokens.append(('str', q[i + 1:end].lower()))
            i = end + 1
            continue
        # Префикс in:/out:/name: (возможно с кавычками)
        m = re.match(r'(in|out|name):\s*(?:"([^"]*)"|(\S*))', q[i:], re.IGNORECASE)
        if m:
            prefix = m.group(1).lower()
            exact  = m.group(2) is not None   # было ли значение в кавычках
            value  = (m.group(2) if exact else m.group(3)).lower()
            tokens.append(('prefix', prefix, value, exact))
            i += m.end()
            continue
        # Обычное слово
        m = re.match(r'[^ \t&|()"]+', q[i:])
        if m:
            tokens.append(('str', m.group(0).lower()))
            i += m.end()
        else:
            i += 1
    return tokens


def _parse_search(q: str):
    """Парсит запрос в AST. Возвращает None при пустом запросе."""
    q = q.strip()
    if not q:
        return None
    # Нет спецсимволов — legacy-режим
    if not re.search(r'(?:in|out|name):|[&|(]', q, re.IGNORECASE):
        exact = q.startswith('"') and q.endswith('"') and len(q) > 2
        value = q[1:-1] if exact else q
        return ('legacy', exact, value.lower())

    tokens = _tokenize_query(q)
    pos = [0]

    def peek():
        return tokens[pos[0]] if pos[0] < len(tokens) else None

    def consume():
        t = tokens[pos[0]]; pos[0] += 1; return t

    def parse_expr():
        left = parse_term()
        while peek() == '|':
            consume()
            right = parse_term()
            left = ('or', left, right)
        return left

    def parse_term():
        left = parse_factor()
        while peek() == '&':
            consume()
            right = parse_factor()
            left = ('and', left, right)
        return left

    def parse_factor():
        if peek() == '(':
            consume()
            node = parse_expr()
            if peek() == ')':
                consume()
            return node
        t = peek()
        if t is None:
            return ('match', None, '')
        consume()
        if isinstance(t, tuple) and t[0] == 'prefix':
            return ('match', t[1], t[2], t[3] if len(t) > 3 else False)
        if isinstance(t, tuple) and t[0] == 'str':
            return ('match', None, t[1])
        return ('match', None, str(t).lower())

    return parse_expr()


def _eval_search(ast, row: dict) -> bool:
    """Проверяет строку данных против AST запроса."""
    if ast is None:
        return True
    kind = ast[0]
    if kind == 'or':
        return _eval_search(ast[1], row) or _eval_search(ast[2], row)
    if kind == 'and':
        return _eval_search(ast[1], row) and _eval_search(ast[2], row)
    if kind == 'legacy':
        _, exact, value = ast
        if exact:
            fields = [row.get('machine_name') or '', row.get('name') or ''] + \
                     [x['item'] for x in (row.get('inputs') or [])] + \
                     [x['item'] for x in (row.get('outputs') or [])]
            return any(f.lower() == value for f in fields if f)
        haystack = ' '.join(filter(None, [
            row.get('machine_name') or '',
            row.get('name') or '',
            ' '.join(x['item'] for x in (row.get('inputs') or [])),
            ' '.join(x['item'] for x in (row.get('outputs') or [])),
        ])).lower()
        return value in haystack
    if kind == 'match':
        prefix = ast[1]; value = ast[2]; exact = ast[3] if len(ast) > 3 else False
        if not value:
            return True
        inputs  = [x['item'].lower() for x in (row.get('inputs')  or [])]
        outputs = [x['item'].lower() for x in (row.get('outputs') or [])]
        name    = (row.get('machine_name') or row.get('name') or '').lower()
        def match(s): return s == value if exact else value in s
        if prefix == 'in':
            return any(match(s) for s in inputs)
        if prefix == 'out':
            return any(match(s) for s in outputs)
        if prefix == 'name':
            return match(name)
        return any(match(s) for s in [name] + inputs + outputs)
    return True

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
import psycopg2
import psycopg2.extras

from auth import (auth_bp, init_oauth, _load_user_from_session,
                  load_guest_by_cookie, create_guest_user)
from db import get_db, dict_cursor

# ─────────────────────────────────────────────────────────────────
# Приложение
# ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]

# OAuth
init_oauth(app)
app.register_blueprint(auth_bp)

# ── Rate limiting (Flask-Limiter) ──────────────────────────────
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=["200 per minute"],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
)
# Лимиты на auth-роуты (blueprint зарегистрирован выше)
limiter.limit("5 per hour")(app.view_functions["auth.email_send"])
limiter.limit("10 per hour")(app.view_functions["auth.email_verify"])
limiter.limit("10 per hour")(app.view_functions["auth.login_with_code"])

# ── Лимит комплексов на пользователя ──────────────────────────
MAX_COMPLEXES = int(os.environ.get("MAX_COMPLEXES_PER_USER", "30"))

# Гостевые сессии
GUEST_COOKIE      = "coi_guest"
GUEST_COOKIE_AGE  = 365 * 24 * 3600  # 1 год


# ─────────────────────────────────────────────────────────────────
# Before-request: загрузить пользователя + язык
# ─────────────────────────────────────────────────────────────────

@app.before_request
def before_request():
    _load_user_from_session()

    # Если нет session-пользователя — ищем / создаём гостевую запись по cookie
    if not g.get("user"):
        cookie_val = request.cookies.get(GUEST_COOKIE)
        if cookie_val:
            g.user = load_guest_by_cookie(cookie_val)
        if not g.get("user"):
            # Новый посетитель: создаём гостя и запоминаем cookie для after_request
            g.user, g._new_guest_cookie = create_guest_user()

    # ?lang= query param — наивысший приоритет (для кнопки смены языка)
    lang_qp = request.args.get("lang")
    if lang_qp and lang_qp in _translations:
        session["lang"] = lang_qp
        if not g.user.get("is_guest"):
            _set_user_setting(g.user["id"], "ui_language", lang_qp)
    # Язык: из сессии → из настроек пользователя → 'en'
    lang = session.get("lang")
    if not lang and not g.user.get("is_guest"):
        lang = _get_user_setting(g.user["id"], "ui_language")
    g.lang = lang or "en"
    # Загружаем переводы игрового контента для текущего языка (один запрос на request)
    g.content_trans = _load_content_translations(g.lang)
    # Тема управляется через localStorage на клиенте
    g.theme = "light"


@app.after_request
def after_request(response):
    """Устанавливает cookie нового гостя если он был создан в этом запросе."""
    new_cookie = getattr(g, "_new_guest_cookie", None)
    if new_cookie:
        response.set_cookie(
            GUEST_COOKIE,
            new_cookie,
            max_age=GUEST_COOKIE_AGE,
            httponly=True,
            samesite="Lax",
        )
    return response


# ─────────────────────────────────────────────────────────────────
# i18n
# ─────────────────────────────────────────────────────────────────

_translations: dict[str, dict] = {}
_i18n_dir = os.path.join(os.path.dirname(__file__), "i18n")

def _load_translations():
    for fname in os.listdir(_i18n_dir):
        if fname.endswith(".json"):
            lang = fname[:-5]
            with open(os.path.join(_i18n_dir, fname), encoding="utf-8") as f:
                _translations[lang] = json.load(f)

_load_translations()



def t(key: str, **kwargs) -> str:
    """Перевод по точечному ключу: t('nav.login'), t('complex.likes', n=5)"""
    lang = getattr(g, "lang", "en")
    parts = key.split(".")
    d = _translations.get(lang) or _translations.get("en", {})
    for p in parts:
        if not isinstance(d, dict):
            return key
        d = d.get(p, key)
    if not isinstance(d, str):
        return key
    for k, v in kwargs.items():
        d = d.replace("{" + k + "}", str(v))
    return d


# Делаем t() доступной в шаблонах
app.jinja_env.globals["t"] = t
app.jinja_env.globals["current_user"] = lambda: g.get("user")


@app.context_processor
def inject_template_globals():
    """Вставить lang, theme, i18n и user в каждый шаблон."""
    lang  = getattr(g, "lang",  "en")
    theme = getattr(g, "theme", "light")
    i18n  = _translations.get(lang) or _translations.get("en", {})
    user  = g.get("user")
    return {"lang": lang, "theme": theme, "i18n": i18n, "user": user}


# ─────────────────────────────────────────────────────────────────
# Переключение языка и темы
# ─────────────────────────────────────────────────────────────────

@app.route("/set-lang/<lang_code>")
def set_lang(lang_code: str):
    if lang_code in _translations:
        session["lang"] = lang_code
        if g.get("user"):
            _set_user_setting(g.user["id"], "ui_language", lang_code)
    return redirect(request.referrer or url_for("index"))


@app.route("/set-theme/<theme_name>")
def set_theme(theme_name: str):
    if theme_name in ("light", "dark"):
        session["theme"] = theme_name
        if g.get("user"):
            _set_user_setting(g.user["id"], "ui_theme", theme_name)
    return redirect(request.referrer or url_for("index"))


# ─────────────────────────────────────────────────────────────────
# Настройки пользователя
# ─────────────────────────────────────────────────────────────────

def _get_user_setting(user_id: int, key: str) -> str | None:
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT value FROM user_settings WHERE user_id = %s AND key = %s",
                (user_id, key),
            )
            row = cur.fetchone()
    return row[0] if row else None


def _set_user_setting(user_id: int, key: str, value: str):
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO user_settings (user_id, key, value) VALUES (%s, %s, %s)
                ON CONFLICT (user_id, key) DO UPDATE SET value = EXCLUDED.value
            """, (user_id, key, value))
        con.commit()


@app.route("/api/settings", methods=["PATCH"])
def api_settings():
    if not g.get("user"):
        return jsonify({"error": "login_required"}), 401
    data = request.get_json(silent=True) or {}
    allowed = {"ui_language", "ui_theme", "show_public_complexes"}
    for key, value in data.items():
        if key in allowed:
            _set_user_setting(g.user["id"], key, str(value))
    # Обновить сессию немедленно для текущего запроса
    if "ui_language" in data:
        session["lang"] = data["ui_language"]
    if "ui_theme" in data:
        session["theme"] = data["ui_theme"]
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────
# Видимость рецептов (персональная, вместо глобального deprecated)
# ─────────────────────────────────────────────────────────────────

def _get_hidden_recipe_ids(user_id: int) -> set[int]:
    """Возвращает множество recipe_id, скрытых данным пользователем."""
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT recipe_id FROM user_recipe_prefs WHERE user_id = %s AND hidden = TRUE",
                (user_id,),
            )
            return {row[0] for row in cur.fetchall()}


@app.route("/api/node/<node_type>/<int:node_id>/hidden", methods=["PATCH"])
def toggle_hidden(node_type: str, node_id: int):
    if node_type not in ("recipe", "complex"):
        return jsonify({"error": "invalid node_type"}), 400
    data   = request.get_json(silent=True) or {}
    hidden = data.get("hidden")
    if not isinstance(hidden, bool):
        return jsonify({"error": "hidden must be bool"}), 400

    # Гости тоже могут скрывать — g.user всегда установлен (guest или real)
    user_id = g.user["id"]

    if node_type == "recipe":
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_recipe_prefs (user_id, recipe_id, hidden)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, recipe_id) DO UPDATE SET hidden = EXCLUDED.hidden
                """, (user_id, node_id, hidden))
            con.commit()
    else:
        # Комплексы не имеют поля deprecated — скрытие через удаление из Browse
        # (комплекс виден только владельцу, поэтому отдельный hide не нужен)
        pass
    return jsonify({"ok": True})


@app.route("/api/nodes/hidden/batch", methods=["PATCH"])
def batch_hidden():
    # Гости тоже могут скрывать
    data   = request.get_json(silent=True) or {}
    hidden = data.get("hidden")
    items  = data.get("items", [])
    if not isinstance(hidden, bool):
        return jsonify({"error": "hidden must be bool"}), 400

    user_id     = g.user["id"]
    recipe_ids  = [x["node_id"] for x in items if x.get("node_type") == "recipe"]
    complex_ids = [x["node_id"] for x in items if x.get("node_type") == "complex"]

    with get_db() as con:
        with con.cursor() as cur:
            for rid in recipe_ids:
                cur.execute("""
                    INSERT INTO user_recipe_prefs (user_id, recipe_id, hidden)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, recipe_id) DO UPDATE SET hidden = EXCLUDED.hidden
                """, (user_id, rid, hidden))
            # Комплексы не имеют поля deprecated — batch hide только для рецептов
        con.commit()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────
# Объединённый запрос рецептов + комплексов
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
        mnt.items       AS maintenance,
        NULL::uuid      AS slug,
        NULL::integer   AS owner_id

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
        FALSE                           AS deprecated,
        inp.items                       AS inputs,
        out.items                       AS outputs,
        mnt_cx.items                    AS maintenance,
        c.slug                          AS slug,
        c.user_id                       AS owner_id

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


def _load_content_translations(lang: str) -> dict[str, str]:
    """Возвращает {english_name: localized_name} для items и buildings.
    Для lang='en' возвращает пустой dict (нет необходимости в переводе).
    """
    if lang == "en":
        return {}
    try:
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT i.name, ct.value
                    FROM items i
                    JOIN content_translations ct ON ct.po_key = i.po_key AND ct.lang = %s
                    WHERE i.po_key IS NOT NULL
                    UNION ALL
                    SELECT b.name, ct.value
                    FROM buildings b
                    JOIN content_translations ct ON ct.po_key = b.po_key AND ct.lang = %s
                    WHERE b.po_key IS NOT NULL
                """, (lang, lang))
                return dict(cur.fetchall())
    except Exception:
        return {}


def _parse_row(row: dict) -> dict:
    row = dict(row)
    # Convert UUID to string for JSON serialisation
    if row.get("slug") is not None:
        row["slug"] = str(row["slug"])
    for f in ("inputs", "outputs", "maintenance"):
        v = row[f]
        if v is None:
            row[f] = []
        elif isinstance(v, str):
            row[f] = json.loads(v)
        if isinstance(row[f], list):
            row[f] = [
                {k: float(val) if isinstance(val, decimal.Decimal) else val
                 for k, val in item.items()}
                for item in row[f]
            ]
    for f in ("cycle_time_s", "workers", "electricity_kw"):
        if row[f] is not None:
            row[f] = float(row[f])
    row["deprecated"] = bool(row.get("deprecated"))

    # Применяем переводы игрового контента (machine_name + item names)
    trans = getattr(g, "content_trans", {})
    if trans:
        if row.get("machine_name"):
            row["machine_name"] = trans.get(row["machine_name"], row["machine_name"])
        for f in ("inputs", "outputs", "maintenance"):
            for entry in row[f]:
                if "item" in entry:
                    entry["item"] = trans.get(entry["item"], entry["item"])

    return row


# ─────────────────────────────────────────────────────────────────
# Страницы
# ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", user=g.get("user"))


@app.route("/complex/new")
def complex_new():
    return render_template("complex_editor.html",
                           complex_id="null",
                           readonly=False,
                           user=g.get("user"))


@app.route("/complex/<slug>/edit")
def complex_edit(slug: str):
    """Редактирование комплекса по UUID-слагу.
    Только владелец может редактировать; остальные перенаправляются на view.
    Анонимный пользователь может редактировать только комплексы без owner (user_id IS NULL).
    """
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("SELECT id, user_id, visibility FROM complexes WHERE slug = %s", (slug,))
            row = cur.fetchone()
    if not row:
        abort(404)
    complex_id, owner_id, visibility = row

    current_uid = g.user["id"] if g.get("user") else None
    # Только владелец редактирует; у анонимов нет прав на чужие
    if owner_id is not None and current_uid != owner_id:
        return redirect(url_for("complex_view", slug=slug))

    return render_template("complex_editor.html",
                           complex_id=complex_id,
                           readonly=False,
                           user=g.get("user"))


@app.route("/complex/<slug>/view")
def complex_view(slug: str):
    """Просмотр комплекса (read-only). Приватные — только для владельца."""
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("SELECT id, user_id, visibility FROM complexes WHERE slug = %s", (slug,))
            row = cur.fetchone()
    if not row:
        abort(404)
    complex_id, owner_id, visibility = row

    # Приватный комплекс → только владелец
    if visibility == "private":
        current_uid = g.user["id"] if g.get("user") else None
        if current_uid != owner_id:
            abort(403)

    return render_template("complex_editor.html",
                           complex_id=complex_id,
                           readonly=True,
                           user=g.get("user"))


# ─────────────────────────────────────────────────────────────────
# API: рецепты / комплексы
# ─────────────────────────────────────────────────────────────────

@app.route("/api/nodes")
def api_nodes():
    q_raw       = request.args.get("q",      "").strip()
    type_filter = request.args.get("type",   "all")
    show_hidden = request.args.get("hidden", "false") == "true"   # true = include hidden items
    page        = max(1, int(request.args.get("page", "1")))
    per_page    = min(100, max(10, int(request.args.get("per_page", "50"))))
    search_ast  = _parse_search(q_raw)

    # Загружаем скрытые рецепты пользователя — всегда (нужно для поля deprecated в ответе)
    hidden_ids: set[int] = _get_hidden_recipe_ids(g.user["id"])

    try:
        with get_db() as con:
            with dict_cursor(con) as cur:
                cur.execute(NODES_SQL)
                rows = [_parse_row(r) for r in cur.fetchall()]
    except Exception as e:
        import traceback
        return jsonify({"error": repr(e), "detail": traceback.format_exc()}), 500

    result = []
    for row in rows:
        if type_filter != "all" and row["node_type"] != type_filter:
            continue

        # Комплексы показываем только свои (owner_id == текущий пользователь)
        if row["node_type"] == "complex" and row["owner_id"] != g.user["id"]:
            continue

        # deprecated для рецептов = только то, что пользователь сам скрыл
        if row["node_type"] == "recipe":
            row["deprecated"] = row["node_id"] in hidden_ids

        # Фильтр: show_hidden=False (default) → скрытые не показываем
        if not show_hidden and row["deprecated"]:
            continue
        if search_ast and not _eval_search(search_ast, row):
            continue
        result.append(row)

    total = len(result)
    start = (page - 1) * per_page
    return jsonify({
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    (total + per_page - 1) // per_page,
        "items":    result[start: start + per_page],
    })


@app.route("/api/nodes/for-resource")
def api_nodes_for_resource():
    item      = request.args.get("item", "").strip()
    direction = request.args.get("direction", "produces")
    type_flt  = request.args.get("type", "all")
    show_hid  = request.args.get("hidden", "false") == "true"

    if not item:
        return jsonify([])

    # ── Фильтр "community": публичные комплексы других пользователей ──
    if type_flt == "community":
        # direction='produces' → ищем комплексы с этим ресурсом на ВЫХОДЕ (rf.direction=1)
        # direction='consumes' → ищем комплексы с этим ресурсом на ВХОДЕ  (rf.direction=0)
        rf_dir = 1 if direction == "produces" else 0
        community_sql = """
            SELECT
                'complex'               AS node_type,
                c.id                    AS node_id,
                c.name                  AS name,
                NULL                    AS machine_name,
                NULL                    AS cycle_time_s,
                c.total_workers         AS workers,
                c.total_electricity_kw  AS electricity_kw,
                FALSE                   AS deprecated,
                inp.items               AS inputs,
                out.items               AS outputs,
                mnt.items               AS maintenance,
                c.slug                  AS slug,
                c.user_id               AS owner_id,
                u.display_name          AS author_name,
                c.likes_count           AS likes_count
            FROM complexes c
            LEFT JOIN users u ON u.id = c.user_id
            LEFT JOIN LATERAL (
                SELECT json_agg(json_build_object('item', i.name, 'qty_per_min', rf.qty_per_min) ORDER BY i.name) AS items
                FROM resource_flows rf JOIN items i ON i.id = rf.item_id
                WHERE rf.parent_type = 1 AND rf.complex_id = c.id AND rf.direction = 0
            ) inp ON TRUE
            LEFT JOIN LATERAL (
                SELECT json_agg(json_build_object('item', i.name, 'qty_per_min', rf.qty_per_min) ORDER BY i.name) AS items
                FROM resource_flows rf JOIN items i ON i.id = rf.item_id
                WHERE rf.parent_type = 1 AND rf.complex_id = c.id AND rf.direction = 1
            ) out ON TRUE
            LEFT JOIN LATERAL (
                SELECT json_agg(json_build_object('item', bm.item, 'rate_per_min', bm.rate_per_min) ORDER BY bm.item) AS items
                FROM building_maintenance bm
                JOIN buildings b ON b.id = bm.building_id
                WHERE FALSE  -- у комплексов нет прямого обслуживания
            ) mnt ON TRUE
            WHERE c.visibility = 'public'
              AND EXISTS (
                  SELECT 1 FROM resource_flows rf2
                  JOIN items i2 ON i2.id = rf2.item_id
                  WHERE rf2.parent_type = 1
                    AND rf2.complex_id  = c.id
                    AND rf2.direction   = %s
                    AND i2.name        = %s
              )
            ORDER BY c.likes_count DESC
            LIMIT 10
        """
        try:
            with get_db() as con:
                with dict_cursor(con) as cur:
                    cur.execute(community_sql, (rf_dir, item))
                    rows = cur.fetchall()
        except Exception as e:
            import traceback
            return jsonify({"error": repr(e), "detail": traceback.format_exc()}), 500

        result = []
        for r in rows:
            row = _parse_row(r)
            row["author_name"]  = r.get("author_name")
            row["likes_count"]  = r.get("likes_count", 0)
            row["is_community"] = True
            result.append(row)
        return jsonify(result)

    # ── Обычный режим: свои рецепты + свои комплексы ──
    hidden_ids: set[int] = _get_hidden_recipe_ids(g.user["id"])

    try:
        with get_db() as con:
            with dict_cursor(con) as cur:
                cur.execute(NODES_SQL)
                rows = [_parse_row(r) for r in cur.fetchall()]
    except Exception as e:
        import traceback
        return jsonify({"error": repr(e), "detail": traceback.format_exc()}), 500

    result = []
    for row in rows:
        if not show_hid:
            if row["node_type"] == "recipe" and row["node_id"] in hidden_ids:
                continue
            if row["node_type"] == "complex" and row["deprecated"]:
                continue
        if type_flt != "all" and row["node_type"] != type_flt:
            continue
        # Для обычного режима — только свои комплексы
        if row["node_type"] == "complex" and row["owner_id"] != g.user["id"]:
            continue
        check = row["outputs"] if direction == "produces" else row["inputs"]
        if any(x["item"] == item for x in check):
            result.append(row)

    return jsonify(result)


@app.route("/api/node/<node_type>/<int:node_id>")
def api_node_detail(node_type: str, node_id: int):
    if node_type not in ("recipe", "complex"):
        return jsonify({"error": "invalid type"}), 400
    try:
        with get_db() as con:
            with dict_cursor(con) as cur:
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


# ─────────────────────────────────────────────────────────────────
# API: комплексы (публичные / мои / форк)
# ─────────────────────────────────────────────────────────────────

@app.route("/api/complexes/public")
def api_public_complexes():
    """Список публичных комплексов с пагинацией и сортировкой."""
    sort    = request.args.get("sort", "new")   # new | popular
    page    = max(1, int(request.args.get("page", "1")))
    per_page = min(50, max(5, int(request.args.get("per_page", "20"))))
    q       = request.args.get("q", "").strip().lower()

    order = "c.likes_count DESC, c.id DESC" if sort == "popular" else "c.id DESC"
    offset = (page - 1) * per_page

    with get_db() as con:
        with dict_cursor(con) as cur:
            where_q = "AND LOWER(c.name) LIKE %s" if q else ""
            params  = [f"%{q}%"] if q else []

            cur.execute(f"""
                SELECT COUNT(*) AS total
                FROM complexes c
                WHERE c.visibility = 'public'
                {where_q}
            """, params)
            total = cur.fetchone()["total"]

            cur.execute(f"""
                SELECT c.id, c.name, c.description, c.likes_count,
                       c.total_workers, c.total_electricity_kw,
                       u.display_name AS author, u.avatar_url AS author_avatar,
                       c.forked_from_id,
                       c.updated_at
                FROM complexes c
                LEFT JOIN users u ON u.id = c.user_id
                WHERE c.visibility = 'public'
                {where_q}
                ORDER BY {order}
                LIMIT %s OFFSET %s
            """, params + [per_page, offset])
            items = [dict(r) for r in cur.fetchall()]

    return jsonify({
        "total":    int(total),
        "page":     page,
        "per_page": per_page,
        "pages":    (int(total) + per_page - 1) // per_page,
        "items":    items,
    })


@app.route("/api/complexes/mine")
def api_my_complexes():
    """Мои комплексы (требует авторизации)."""
    if not g.get("user"):
        return jsonify({"error": "login_required"}), 401

    with get_db() as con:
        with dict_cursor(con) as cur:
            cur.execute("""
                SELECT c.id, c.name, c.description, c.visibility,
                       c.likes_count, c.total_workers, c.total_electricity_kw,
                       c.forked_from_id, c.updated_at
                FROM complexes c
                WHERE c.user_id = %s
                ORDER BY c.id DESC
            """, (g.user["id"],))
            items = [dict(r) for r in cur.fetchall()]

    return jsonify(items)


@app.route("/api/complex/<int:complex_id>/visibility", methods=["PATCH"])
def api_complex_visibility(complex_id: int):
    """Изменить видимость комплекса (private ↔ public)."""
    data = request.get_json(silent=True) or {}
    visibility = data.get("visibility")
    if visibility not in ("private", "public"):
        return jsonify({"error": "visibility must be private or public"}), 400

    # Гости не могут публиковать в Community — нужна реальная авторизация
    if visibility == "public" and g.user.get("is_guest"):
        return jsonify({"error": "login_required", "reason": "publish"}), 401

    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(
                "UPDATE complexes SET visibility = %s WHERE id = %s AND user_id = %s RETURNING id",
                (visibility, complex_id, g.user["id"]),
            )
            if not cur.fetchone():
                return jsonify({"error": "not found or forbidden"}), 404
        con.commit()
    return jsonify({"ok": True, "visibility": visibility})


@app.route("/api/complex/<int:complex_id>/fork", methods=["POST"])
@limiter.limit("10 per hour")
def api_complex_fork(complex_id: int):
    """Скопировать публичный комплекс в аккаунт пользователя."""
    if g.user.get("is_guest"):
        return jsonify({"error": "login_required", "reason": "fork"}), 401

    # Проверить суммарный лимит комплексов
    if not g.user.get("is_premium"):
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM complexes WHERE user_id = %s",
                    (g.user["id"],),
                )
                count = cur.fetchone()[0]
        if count >= MAX_COMPLEXES:
            return jsonify({
                "error":   "limit_reached",
                "limit":   MAX_COMPLEXES,
                "message": f"Maximum {MAX_COMPLEXES} complexes per account. Delete old ones to create new.",
            }), 403

    with get_db() as con:
        with con.cursor() as cur:
            # Проверить что оригинал публичный
            cur.execute(
                "SELECT id, name FROM complexes WHERE id = %s AND visibility = 'public'",
                (complex_id,),
            )
            orig = cur.fetchone()
            if not orig:
                return jsonify({"error": "not found or not public"}), 404

            # Создать копию
            cur.execute("""
                INSERT INTO complexes (name, user_id, visibility, forked_from_id)
                VALUES (%s, %s, 'private', %s)
                RETURNING id
            """, (f"[Copy] {orig[1]}", g.user["id"], complex_id))
            new_id = cur.fetchone()[0]

            # Скопировать члены
            cur.execute("""
                INSERT INTO complex_members
                    (complex_id, child_type, child_id, recipe_id, child_complex_id,
                     multiplier, pos_x, pos_y, efficiency, idle_item, idle_direction,
                     is_manual_partial)
                SELECT %s, child_type, child_id, recipe_id, child_complex_id,
                       multiplier, pos_x, pos_y, efficiency, idle_item, idle_direction,
                       is_manual_partial
                FROM complex_members
                WHERE complex_id = %s
                RETURNING id
            """, (new_id, complex_id))
            # (рёбра требуют маппинга старых → новых member_id; для упрощения — пересчитаем)
            cur.execute("SELECT recalculate_complex(%s)", (new_id,))
            cur.execute("SELECT slug FROM complexes WHERE id = %s", (new_id,))
            new_slug = str(cur.fetchone()[0])
        con.commit()

    return jsonify({"ok": True, "id": new_id, "slug": new_slug}), 201


@app.route("/api/complex/<int:complex_id>/like", methods=["POST", "DELETE"])
@limiter.limit("60 per hour")
def api_complex_like(complex_id: int):
    """Поставить / убрать лайк."""
    if not g.get("user"):
        return jsonify({"error": "login_required"}), 401
    user_id = g.user["id"]
    with get_db() as con:
        with con.cursor() as cur:
            if request.method == "POST":
                cur.execute("""
                    INSERT INTO complex_likes (user_id, complex_id)
                    VALUES (%s, %s) ON CONFLICT DO NOTHING
                """, (user_id, complex_id))
            else:
                cur.execute(
                    "DELETE FROM complex_likes WHERE user_id = %s AND complex_id = %s",
                    (user_id, complex_id),
                )
        con.commit()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────
# API: граф комплекса (перенесён из локальной версии без изменений)
# ─────────────────────────────────────────────────────────────────

def _parse_json_list(v):
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
            with dict_cursor(con) as cur:
                cur.execute(
                    "SELECT id, name, description, user_id, visibility, forked_from_id, likes_count FROM complexes WHERE id = %s",
                    (complex_id,),
                )
                cx = cur.fetchone()
                if not cx:
                    return jsonify({"error": "not found"}), 404

                # Доступ: публичный — всем; приватный — только владельцу
                cx = dict(cx)
                if cx["visibility"] == "private":
                    uid = g.user["id"] if g.get("user") else None
                    if uid != cx["user_id"]:
                        return jsonify({"error": "forbidden"}), 403

                cur.execute("""
                    SELECT
                        cm.id, cm.child_type, cm.child_id,
                        cm.multiplier, cm.pos_x, cm.pos_y,
                        cm.efficiency, cm.idle_item, cm.idle_direction, cm.is_manual_partial,
                        cm.external_ports,
                        r.machine_name,
                        b.workers, b.electricity_kw,
                        c2.name  AS complex_name,
                        c2.slug  AS complex_slug,
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

                cur.execute("""
                    SELECT id, from_member_id, to_member_id, resource_item, lcm_mode
                    FROM   complex_edges
                    WHERE  complex_id = %s
                    ORDER  BY id
                """, (complex_id,))
                edges = [dict(e) for e in cur.fetchall()]

                sub_ids = [m["child_id"] for m in members if m["child_type"] == 1]
                sub_flows: dict[int, dict] = {}
                if sub_ids:
                    cur.execute("""
                        SELECT rf.complex_id, rf.direction, i.name AS item, rf.qty_per_min
                        FROM   resource_flows rf
                        JOIN   items i ON i.id = rf.item_id
                        WHERE  rf.parent_type = 1 AND rf.complex_id = ANY(%s)
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
                inp_list, out_list, mnt_list = sf["inputs"], sf["outputs"], []
            else:
                inp_list = _parse_json_list(m["inputs"])
                out_list = _parse_json_list(m["outputs"])
                mnt_list = _parse_json_list(m["maintenance"])

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
                "label":            m["machine_name"] or m["complex_name"] or "?",
                "node_ref_slug":    str(m["complex_slug"]) if m.get("complex_slug") else None,
                "workers":          _f(m["workers"]) if m["workers"] is not None else None,
                "electricity_kw":   _f(m["electricity_kw"]) if m["electricity_kw"] is not None else None,
                "inputs":           inp_list,
                "outputs":          out_list,
                "maintenance":      mnt_list,
            })

        return jsonify({
            "id":             cx["id"],
            "name":           cx["name"],
            "description":    cx["description"],
            "visibility":     cx["visibility"],
            "forked_from_id": cx.get("forked_from_id"),
            "likes_count":    cx.get("likes_count", 0),
            "is_owner":       g.get("user") and g.user["id"] == cx["user_id"],
            "nodes":          nodes,
            "edges":          edges,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": repr(e), "detail": traceback.format_exc()}), 500


# ─────────────────────────────────────────────────────────────────
# API: сохранение комплекса
# ─────────────────────────────────────────────────────────────────

def _save_complex_graph(con, complex_id, data, user_id: int | None):
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")

    nodes_data = data.get("nodes", [])
    edges_data = data.get("edges", [])

    with con.cursor() as cur:
        if complex_id:
            cur.execute(
                "UPDATE complexes SET name = %s WHERE id = %s RETURNING id, user_id",
                (name, complex_id),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("complex not found")
            if user_id and row[1] != user_id:
                raise PermissionError("forbidden")
        else:
            # Проверить лимит
            if user_id:
                cur.execute(
                    "SELECT COUNT(*) FROM complexes WHERE user_id = %s",
                    (user_id,),
                )
                count = cur.fetchone()[0]
                # is_premium проверяется выше в роуте
            cur.execute(
                "INSERT INTO complexes (name, user_id) VALUES (%s, %s) RETURNING id",
                (name, user_id),
            )
            complex_id = cur.fetchone()[0]

        cur.execute("DELETE FROM complex_members WHERE complex_id = %s", (complex_id,))

        id_map: dict[str, int] = {}
        for i, nd in enumerate(nodes_data):
            child_type        = 0 if nd["node_type"] == "recipe" else 1
            ref_id            = int(nd["node_ref_id"])
            is_manual_partial = bool(nd.get("is_manual_partial", False))
            count             = max(1, int(round(float(nd.get("count", 1)))))
            pos_x             = int(nd.get("pos_x", i * 380))
            pos_y             = int(nd.get("pos_y", 100))
            efficiency        = max(0.0001, min(1.0, float(nd.get("efficiency", 1.0))))
            idle_item         = nd.get("idle_item") or None
            idle_direction    = nd.get("idle_direction") or None
            ext_ports_raw     = nd.get("external_ports") or []
            external_ports    = json.dumps(ext_ports_raw) if ext_ports_raw else None

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

        cur.execute("SELECT recalculate_complex(%s)", (complex_id,))

        # Remove noise flows after recalculation
        # 1. Idle-port resource: user explicitly balanced it to zero
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

        # 2. Manual partial nodes: fractional count causes noise; no meaningful flow < 1/min in CoI
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
@limiter.limit("30 per hour")   # rate: не более 30 новых комплексов в час с одного IP
def api_complex_create():
    # Гости тоже могут создавать комплексы (g.user всегда установлен)
    # Проверить суммарный лимит (premium пользователи без ограничений)
    if not g.user.get("is_premium"):
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM complexes WHERE user_id = %s",
                    (g.user["id"],),
                )
                count = cur.fetchone()[0]
        if count >= MAX_COMPLEXES:
            return jsonify({
                "error":   "limit_reached",
                "limit":   MAX_COMPLEXES,
                "message": f"Maximum {MAX_COMPLEXES} complexes per account. Delete old ones to create new.",
            }), 403

    data = request.get_json(silent=True) or {}
    try:
        with get_db() as con:
            cid = _save_complex_graph(con, None, data, g.user["id"])
            with con.cursor() as cur:
                cur.execute("SELECT slug FROM complexes WHERE id = %s", (cid,))
                slug_row = cur.fetchone()
            con.commit()
        slug = str(slug_row[0]) if slug_row else None
        return jsonify({"ok": True, "id": cid, "slug": slug}), 201
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "A complex with this name already exists"}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        return jsonify({"error": repr(e), "detail": traceback.format_exc()}), 500


@app.route("/api/complex/<int:complex_id>", methods=["PUT"])
def api_complex_update(complex_id: int):
    if not g.get("user"):
        return jsonify({"error": "login_required"}), 401
    data = request.get_json(silent=True) or {}
    try:
        with get_db() as con:
            _save_complex_graph(con, complex_id, data, g.user["id"])
            con.commit()
        return jsonify({"ok": True, "id": complex_id})
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "A complex with this name already exists"}), 409
    except (ValueError, PermissionError) as e:
        code = 403 if isinstance(e, PermissionError) else 400
        return jsonify({"error": str(e)}), code
    except Exception as e:
        import traceback
        return jsonify({"error": repr(e), "detail": traceback.format_exc()}), 500


@app.route("/api/complex/<int:complex_id>", methods=["DELETE"])
def api_complex_delete(complex_id: int):
    if not g.get("user"):
        return jsonify({"error": "login_required"}), 401
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM complexes WHERE id = %s AND user_id = %s RETURNING id",
                (complex_id, g.user["id"]),
            )
            if not cur.fetchone():
                return jsonify({"error": "not found or forbidden"}), 404
        con.commit()
    return jsonify({"ok": True})


@app.route("/api/complex/<int:complex_id>/members")
def api_complex_members(complex_id: int):
    try:
        with get_db() as con:
            with dict_cursor(con) as cur:
                cur.execute("""
                    SELECT
                        CASE cm.child_type WHEN 0 THEN 'recipe' ELSE 'complex' END AS node_type,
                        COALESCE(r.machine_name, c2.name, '?')                    AS label,
                        cm.multiplier                                              AS count,
                        COALESCE(cm.efficiency, 1.0)                              AS efficiency,
                        COALESCE(b.workers,        c2.total_workers)              AS workers,
                        COALESCE(b.electricity_kw, c2.total_electricity_kw)       AS electricity_kw
                    FROM  complex_members cm
                    LEFT  JOIN recipes   r  ON r.id  = cm.recipe_id
                    LEFT  JOIN buildings b  ON b.id  = r.machine_id
                    LEFT  JOIN complexes c2 ON c2.id = cm.child_complex_id
                    WHERE cm.complex_id = %s
                    ORDER BY label
                """, (complex_id,))
                rows = cur.fetchall()
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
        return jsonify({"error": repr(e), "detail": traceback.format_exc()}), 500


# ─────────────────────────────────────────────────────────────────
# CLI-команды (flask <cmd>)
# ─────────────────────────────────────────────────────────────────

import click

@app.cli.command("cleanup-guests")
@click.option("--months", default=6, show_default=True,
              help="Удалить гостей без активности дольше N месяцев.")
@click.option("--dry-run", is_flag=True,
              help="Показать сколько будет удалено, но не удалять.")
def cleanup_guests(months: int, dry_run: bool) -> None:
    """Удаляет гостевые аккаунты без активности дольше заданного срока.

    Пример запуска вручную:
        flask cleanup-guests
        flask cleanup-guests --months 3
        flask cleanup-guests --dry-run

    Пример cron (каждое воскресенье в 3:00):
        0 3 * * 0  cd /app && flask cleanup-guests >> /var/log/coi_cleanup.log 2>&1
    """
    sql_count = """
        SELECT COUNT(*) FROM users
        WHERE  is_guest = TRUE
          AND  last_seen_at < NOW() - (%s || ' months')::INTERVAL
    """
    sql_delete = """
        DELETE FROM users
        WHERE  is_guest = TRUE
          AND  last_seen_at < NOW() - (%s || ' months')::INTERVAL
        RETURNING id
    """
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(sql_count, (str(months),))
            count = cur.fetchone()[0]

        if dry_run:
            click.echo(f"[dry-run] Будет удалено {count} гостевых аккаунтов "
                       f"без активности >{months} мес.")
            return

        if count == 0:
            click.echo(f"Нет гостей без активности >{months} мес. Ничего не удалено.")
            return

        with con.cursor() as cur:
            cur.execute(sql_delete, (str(months),))
            deleted_ids = [r[0] for r in cur.fetchall()]
        con.commit()

    click.echo(f"Удалено {len(deleted_ids)} гостевых аккаунтов "
               f"(inactive >{months} мес.). IDs: {deleted_ids[:10]}"
               f"{'...' if len(deleted_ids) > 10 else ''}")


# ─────────────────────────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import glob
    # Следим за .json файлами переводов — при изменении dev-сервер перезапустится
    extra = glob.glob(os.path.join(_i18n_dir, "*.json"))
    app.run(debug=True, port=5001, extra_files=extra)
