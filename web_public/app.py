"""
Captain of Industry — публичная веб-версия
────────────────────────────────────────────────────────────────────
Установка:  pip install -r requirements.txt
Настройка:  скопировать .env.example → .env и заполнить
Запуск:     python app.py
Браузер:    http://localhost:5001
"""
from __future__ import annotations

import datetime as _dt
from datetime import timedelta
import decimal
import functools
import json
import os
import re
import traceback
import uuid as _uuid

from dotenv import load_dotenv
load_dotenv()  # загрузить .env до инициализации Flask


_MNT_TIER_RE = re.compile(r'(\d+)$')

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
        # Shorthand: #tagname → tag:tagname
        if c == '#':
            m = re.match(r'#([\w\-]{1,30})', q[i:], re.IGNORECASE | re.UNICODE)
            if m:
                tokens.append(('prefix', 'tag', m.group(1).lower(), True))
                i += m.end()
                continue
        # Префикс in:/out:/name:/by:/tag: (возможно с кавычками)
        m = re.match(r'(in|out|name|by|tag):\s*(?:"([^"]*)"|(\S*))', q[i:], re.IGNORECASE)
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
    if not re.search(r'(?:in|out|name|by|tag):|#[a-z0-9\-]|[&|(]', q, re.IGNORECASE):
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
        if prefix == 'tag':
            tags = [t.lower() for t in (row.get('tags') or [])]
            return any(match(t) for t in tags)
        return any(match(s) for s in [name] + inputs + outputs)
    return True

def _build_community_where(ast, params: list, trans_rev: dict) -> tuple:
    """Рекурсивно строит SQL-фрагмент WHERE для community-поиска.
    Возвращает (sql_fragment, needs_users_join).
    params мутируется на месте.
    """
    if ast is None:
        return "1=1", False

    kind = ast[0]

    if kind in ("or", "and"):
        op = "OR" if kind == "or" else "AND"
        l, lj = _build_community_where(ast[1], params, trans_rev)
        r, rj = _build_community_where(ast[2], params, trans_rev)
        return f"({l} {op} {r})", lj or rj

    if kind == "legacy":
        _, exact, value = ast
        value_db = trans_rev.get(value, value).lower()   # RU→EN + lowercase для ресурсов
        res_sub = ("EXISTS (SELECT 1 FROM resource_flows rf2 "
                   "JOIN items i2 ON i2.id = rf2.item_id "
                   "WHERE rf2.parent_type = 1 AND rf2.parent_id = c.id "
                   "AND LOWER(i2.name) {op} %s)")
        if exact:
            params += [value, value_db]
            return f"(LOWER(c.name) = %s OR {res_sub.format(op='=')})", False
        params += [f"%{value}%", f"%{value_db}%"]
        return f"(LOWER(c.name) LIKE %s OR {res_sub.format(op='LIKE')})", False

    if kind == "match":
        prefix = ast[1]
        value  = ast[2]
        exact  = ast[3] if len(ast) > 3 else False
        if not value:
            return "1=1", False

        if prefix == "by":
            op = "=" if exact else "LIKE"
            v  = value if exact else f"%{value}%"
            params.append(v)
            return f"LOWER(u.display_name) {op} %s", True

        if prefix in ("in", "out"):
            direction = 0 if prefix == "in" else 1
            value_db  = trans_rev.get(value, value).lower()   # RU→EN + lowercase для LOWER()
            op = "=" if exact else "LIKE"
            v  = value_db if exact else f"%{value_db}%"
            params.append(v)
            sql = (f"EXISTS (SELECT 1 FROM resource_flows rf2 "
                   f"JOIN items i2 ON i2.id = rf2.item_id "
                   f"WHERE rf2.parent_type = 1 AND rf2.parent_id = c.id "
                   f"AND rf2.direction = {direction} "
                   f"AND LOWER(i2.name) {op} %s)")
            return sql, False

        if prefix == "tag":
            op = "=" if exact else "LIKE"
            v  = value if exact else f"%{value}%"
            params.append(v)
            return (
                "EXISTS (SELECT 1 FROM complex_tags ct "
                "JOIN tags tg ON tg.id = ct.tag_id "
                f"WHERE ct.complex_id = c.id AND tg.name {op} %s)"
            ), False

        # name: или None → поиск по названию комплекса
        op = "=" if exact else "LIKE"
        v  = value if exact else f"%{value}%"
        params.append(v)
        return f"LOWER(c.name) {op} %s", False

    return "1=1", False


from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
import psycopg2
import psycopg2.extras

from auth import (auth_bp, init_oauth, _load_user_from_session,
                  load_guest_by_cookie, create_guest_user)
from db import get_db, dict_cursor

# ─────────────────────────────────────────────────────────────────
# Приложение
# ─────────────────────────────────────────────────────────────────

# Flask 3.0 сериализует datetime как RFC 1123 (http_date).
# Переопределяем провайдер, чтобы datetime → ISO 8601 (нужно для relativeTime в JS).
from flask.json.provider import DefaultJSONProvider as _DefaultJSONProvider

class _ISOJSONProvider(_DefaultJSONProvider):
    @staticmethod
    def default(o: object) -> object:  # type: ignore[override]
        if isinstance(o, _dt.datetime):
            return o.isoformat()
        if isinstance(o, _dt.date):
            return o.isoformat()
        return _DefaultJSONProvider.default(o)  # type: ignore[arg-type]

app = Flask(__name__)
app.json_provider_class = _ISOJSONProvider
app.json = _ISOJSONProvider(app)
app.secret_key = os.environ["SECRET_KEY"]
app.permanent_session_lifetime = timedelta(days=30)   # сессия живёт 30 дней после закрытия браузера

# За обратным прокси (Nginx) доверяем X-Forwarded-* — иначе request.url/host_url
# и _external=True (OAuth-редиректы, og:url) формируются с http и внутренним хостом.
# Включается только в проде: TRUST_PROXY=1 в .env.
if os.environ.get("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# OAuth
init_oauth(app)
app.register_blueprint(auth_bp)

# Серверные страницы справочника + robots.txt/sitemap.xml (SEO-слой)
from seo import (seo_bp, item_url as _seo_item_url,
                 building_url as _seo_building_url, content_counts as _content_counts)

import monitoring as _monitoring

app.register_blueprint(seo_bp)
app.jinja_env.globals["seo_item_url"]     = _seo_item_url
app.jinja_env.globals["seo_building_url"] = _seo_building_url
app.jinja_env.globals["loc_name"] = \
    lambda name: (getattr(g, "content_trans", None) or {}).get(name, name)

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
# Справочник обходят краулеры: сотни страниц подряд. Дефолтные 200/мин отдали бы
# Googlebot пачку 429, а это режет скорость обхода. Свой щедрый лимит вместо дефолта.
limiter.limit("600 per minute")(seo_bp)

limiter.limit("5 per hour")(app.view_functions["auth.email_send"])
limiter.limit("10 per hour")(app.view_functions["auth.email_verify"])
limiter.limit("10 per hour")(app.view_functions["auth.login_with_code"])

# ── Лимиты комплексов ─────────────────────────────────────────
MAX_COMPLEXES       = int(os.environ.get("MAX_COMPLEXES_PER_USER", "0"))  # 0 = без лимита
# Гости: не более N комплексов с одного IP или fingerprint за 30 дней
GUEST_MAX_PER_IP    = int(os.environ.get("GUEST_MAX_COMPLEXES_PER_IP", "10"))
GUEST_MAX_PER_FP    = int(os.environ.get("GUEST_MAX_COMPLEXES_PER_FP", "10"))
# Аварийный клапан: если всего гостевых комплексов > порога — блокируем создание для гостей
GUEST_GLOBAL_CAP    = int(os.environ.get("GUEST_GLOBAL_COMPLEX_CAP", "5000"))

# Гостевые сессии
GUEST_COOKIE      = "coi_guest"
GUEST_COOKIE_AGE  = 365 * 24 * 3600  # 1 год

# Псевдопользователь для краулеров: не пишется в users, id=None.
# Запросы вида «WHERE user_id = NULL» не находят ничего — это и нужно.
ANON_USER = {
    "id":           None,
    "display_name": "Crawler",
    "avatar_url":   None,
    "email":        None,
    "is_premium":   False,
    "is_guest":     True,
}

# «bot» как подстрока задевает настоящие устройства (Android CUBOT_X30), а ложное
# срабатывание стоит дорого: живой игрок остался бы без гостевой записи и не смог
# бы ничего сохранить. Поэтому «bot» — только с разделителем после (Googlebot/2.1).
_CRAWLER_UA_RE = re.compile(
    r"bot[/;)\s]|bot$|crawler|spider|slurp|"
    r"facebookexternalhit|embedly|whatsapp|telegrambot|skypeuripreview|"
    r"lighthouse|pingdom|"
    r"curl/|wget/|python-requests|python-httpx|go-http-client|okhttp|"
    r"headlesschrome|phantomjs",
    re.IGNORECASE,
)


def _is_crawler() -> bool:
    """Поисковый робот или превью-краулер: гостевую запись для него не создаём.

    Боты не хранят cookie, поэтому каждая страница обхода заводила бы нового
    гостя — при 350 страницах справочника это мусор в users на каждый обход.
    """
    return bool(_CRAWLER_UA_RE.search(request.headers.get("User-Agent", "")))


def _err500(e: Exception):
    return jsonify({"error": repr(e), "detail": traceback.format_exc()}), 500


# ─────────────────────────────────────────────────────────────────
# Before-request: загрузить пользователя + язык
# ─────────────────────────────────────────────────────────────────

@app.before_request
def before_request():
    _load_user_from_session()

    # Постоянная сессия для залогиненных (чтобы куки не слетали при закрытии браузера)
    if g.get("user") and not g.user.get("is_guest"):
        session.permanent = True

    # Если нет session-пользователя — ищем / создаём гостевую запись по cookie
    if not g.get("user"):
        cookie_val = request.cookies.get(GUEST_COOKIE)
        if cookie_val:
            g.user = load_guest_by_cookie(cookie_val)
        if not g.get("user") and not _is_crawler():
            # Новый посетитель: создаём гостя (с источником перехода) и запоминаем cookie
            g.user, g._new_guest_cookie = create_guest_user(
                request.args.get("utm_source"), request.referrer)
        if not g.get("user"):
            # Краулер: работаем без записи в users — иначе каждая страница
            # обхода плодит гостя (боты не хранят cookie).
            g.user = ANON_USER

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


# Доступность OAuth-провайдеров вычисляется один раз при старте по наличию credentials.
# Google регистрируется в init_oauth() только при заданных id+secret — без них роут
# /auth/google/login упал бы с 500, поэтому кнопку прячем. Steam-вход технически
# работает и без ключа, но без него имя берётся как "Steam:xxxxxxxx" — поэтому
# показываем кнопку только когда ключ задан.
GOOGLE_ENABLED = bool(os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))
STEAM_ENABLED  = bool(os.environ.get("STEAM_API_KEY"))

# ─────────────────────────────────────────────────────────────────
# Админ-доступ
# ─────────────────────────────────────────────────────────────────
# Список почт через запятую в ADMIN_EMAILS. Пусто — админки нет вообще:
# это безопасное значение по умолчанию, случайно открытой она не окажется.
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("ADMIN_EMAILS", "").split(",")
    if e.strip()
}


def _is_admin() -> bool:
    user = g.get("user")
    if not user or user.get("is_guest") or not user.get("email"):
        return False
    # Обе стороны приводим к нижнему регистру здесь же: полагаться на то, что
    # ADMIN_EMAILS уже нормализован, — способ однажды не пустить самого себя.
    return user["email"].strip().lower() in {e.strip().lower() for e in ADMIN_EMAILS}


def admin_required(f):
    """Пускает только админов. Остальным — 404, а не 403.

    404 не подтверждает, что роут существует: по 403 админку можно найти
    перебором, по 404 — нет.
    """
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not _is_admin():
            abort(404)
        return f(*args, **kwargs)
    return wrapper


# Ссылки на донаты (переопределяются через .env при необходимости)
DONATE_LAVA_URL   = os.environ.get("DONATE_LAVA_URL",   "https://app.lava.top/products/4f9189e1-9e48-4d01-964d-5e5765adacb7")
DONATE_BOOSTY_URL = os.environ.get("DONATE_BOOSTY_URL", "https://boosty.to/burba_vladimir/donate")


@app.context_processor
def inject_template_globals():
    """Вставить lang, theme, i18n, user, флаги OAuth и ссылки донатов в каждый шаблон."""
    lang  = getattr(g, "lang",  "en")
    theme = getattr(g, "theme", "light")
    i18n  = _translations.get(lang) or _translations.get("en", {})
    user  = g.get("user")
    # Пройденные туры: у авторизованных берём с сервера, чтобы гайд не
    # выскакивал заново на другом устройстве. Гостям хватает localStorage.
    tour_seen = ""
    if user and not user.get("is_guest"):
        try:
            tour_seen = _get_user_setting(user["id"], "tour_seen") or ""
        except Exception:
            tour_seen = ""
    return {
        "lang": lang, "theme": theme, "i18n": i18n, "user": user, "tour_seen": tour_seen,
        "google_enabled": GOOGLE_ENABLED, "steam_enabled": STEAM_ENABLED,
        "donate_lava_url": DONATE_LAVA_URL, "donate_boosty_url": DONATE_BOOSTY_URL,
        "is_admin": _is_admin(),
    }


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
    # population_prefs — выбранные рецепты, уровни зданий и население
    # на странице нужд поселения. Хранится JSON-строкой.
    # tour_seen — какие обучающие туры пользователь уже прошёл, через запятую.
    allowed = {"ui_language", "ui_theme", "show_public_complexes", "population_prefs",
               "tour_seen"}
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


def _get_hidden_complex_ids(user_id: int) -> set[int]:
    """Возвращает множество complex_id, скрытых данным пользователем."""
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT complex_id FROM user_complex_prefs WHERE user_id = %s AND hidden = TRUE",
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
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_complex_prefs (user_id, complex_id, hidden)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, complex_id) DO UPDATE SET hidden = EXCLUDED.hidden
                """, (user_id, node_id, hidden))
            con.commit()
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
            for cid in complex_ids:
                cur.execute("""
                    INSERT INTO user_complex_prefs (user_id, complex_id, hidden)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (user_id, complex_id) DO UPDATE SET hidden = EXCLUDED.hidden
                """, (user_id, cid, hidden))
        con.commit()
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────
# Объединённый запрос рецептов + комплексов
# ─────────────────────────────────────────────────────────────────

def _make_nodes_sql(user_id: int) -> tuple[str, tuple]:
    """SQL для рецептов + своих/подписанных комплексов текущего пользователя.

    Комплексы фильтруются на уровне БД: только собственные и подписанные.
    Поле _subscribed=True означает «чужой, но подписан».
    """
    sql = """\
SELECT * FROM (

    SELECT
        'recipe'        AS node_type,
        r.id            AS node_id,
        NULL            AS name,
        r.machine_name,
        r.cycle_time_s,
        b.workers,
        b.electricity_kw * COALESCE(r.power_multiplier, 1.0) AS electricity_kw,
        b.computing_tf,
        r.deprecated,
        inp.items       AS inputs,
        out.items       AS outputs,
        mnt.items       AS maintenance,
        constr.items    AS construction,
        NULL::uuid      AS slug,
        NULL::integer   AS owner_id,
        NULL::text      AS visibility,
        NULL::json      AS tags,
        FALSE           AS _subscribed

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

    LEFT JOIN LATERAL (
        SELECT json_agg(
                   json_build_object('item', bc.item, 'qty', bc.qty)
                   ORDER BY bc.item
               ) AS items
        FROM  building_construction bc
        WHERE bc.building_id = b.id
    ) constr ON TRUE

    UNION ALL

    SELECT
        'complex'                               AS node_type,
        c.id                                    AS node_id,
        c.name                                  AS name,
        NULL                                    AS machine_name,
        NULL                                    AS cycle_time_s,
        c.total_workers                         AS workers,
        c.total_electricity_kw                  AS electricity_kw,
        COALESCE(c.total_computing_tf, 0)       AS computing_tf,
        FALSE                                   AS deprecated,
        inp.items                               AS inputs,
        out.items                               AS outputs,
        mnt_cx.items                            AS maintenance,
        cx_constr.items                         AS construction,
        c.slug                                  AS slug,
        c.user_id                               AS owner_id,
        c.visibility::text                      AS visibility,
        cx_tags.tags                            AS tags,
        (c.user_id IS DISTINCT FROM %s)         AS _subscribed

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

    LEFT JOIN LATERAL (
        SELECT json_agg(json_build_object('item', cc.item, 'qty', cc.qty) ORDER BY cc.item) AS items
        FROM   complex_construction cc
        WHERE  cc.complex_id = c.id
    ) cx_constr ON TRUE

    LEFT JOIN LATERAL (
        SELECT COALESCE(json_agg(tg.name ORDER BY tg.name), '[]'::json) AS tags
        FROM  complex_tags ct2
        JOIN  tags tg ON tg.id = ct2.tag_id
        WHERE ct2.complex_id = c.id
    ) cx_tags ON TRUE

    WHERE c.is_ghost = FALSE
      AND (
          c.user_id = %s
          OR EXISTS (
              SELECT 1 FROM complex_subscriptions cs
              WHERE cs.complex_id = c.id AND cs.user_id = %s
          )
      )

) _nodes
ORDER BY COALESCE(machine_name, name)
"""
    return sql, (user_id, user_id, user_id)


@functools.lru_cache(maxsize=8)
def _load_content_translations(lang: str) -> dict[str, str]:
    """Возвращает {english_name: localized_name} для items и buildings.
    Результат кешируется в памяти процесса — переводы не меняются во время работы.
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


def _add_item_display(items: list) -> list:
    """Добавляет item_display (перевод) не трогая item (английский канон для матчинга/сохранения)."""
    trans = getattr(g, "content_trans", {})
    if not trans:
        return items
    result = []
    for entry in items:
        e = dict(entry)
        if "item" in e and e["item"] in trans:
            e["item_display"] = trans[e["item"]]
        result.append(e)
    return result


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
    # computing_tf: float or None
    if row.get("computing_tf") is not None:
        row["computing_tf"] = float(row["computing_tf"])
    # construction: one-time build cost items list
    v = row.get("construction")
    if v is None:
        row["construction"] = []
    elif isinstance(v, str):
        row["construction"] = json.loads(v)
    if isinstance(row.get("construction"), list):
        row["construction"] = [
            {k: float(val) if isinstance(val, decimal.Decimal) else val
             for k, val in item.items()}
            for item in row["construction"]
        ]
    row["deprecated"] = bool(row.get("deprecated"))

    # Tags: psycopg2 may return already-parsed list, a JSON string, or None
    v = row.get("tags")
    if v is None:
        row["tags"] = []
    elif isinstance(v, str):
        row["tags"] = json.loads(v)
    # else already a list (psycopg2 json adapter)

    # Применяем переводы игрового контента (machine_name + item names)
    trans = getattr(g, "content_trans", {})
    if trans:
        if row.get("machine_name"):
            row["machine_name"] = trans.get(row["machine_name"], row["machine_name"])
        for f in ("inputs", "outputs", "maintenance", "construction"):
            for entry in row[f]:
                if "item" in entry:
                    entry["item_en"] = entry["item"]
                    entry["item"] = trans.get(entry["item"], entry["item"])

    return row


# ─────────────────────────────────────────────────────────────────
# Страницы
# ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    # counts — для серверного текста на главной: числа в лендинге должны быть настоящими
    return render_template("index.html", user=g.get("user"), counts=_content_counts())


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/population")
def population():
    return render_template("population.html", user=g.get("user"))


# Демо-комплекс для обучающего тура по редактору: на пустом холсте
# показывать нечего. Слаг можно закрепить через .env, иначе берём самый
# популярный публичный комплекс — он всегда есть и не требует ухода.
TOUR_DEMO_SLUG = os.environ.get("TOUR_DEMO_SLUG", "")


@app.route("/tour/editor")
def tour_editor():
    slug = TOUR_DEMO_SLUG
    if not slug:
        try:
            with get_db() as con:
                with con.cursor() as cur:
                    cur.execute("""
                        SELECT slug FROM complexes
                        WHERE visibility = 'public' AND is_ghost = FALSE
                        ORDER BY likes_count DESC, id DESC
                        LIMIT 1
                    """)
                    row = cur.fetchone()
            slug = row[0] if row else ""
        except Exception:
            slug = ""
    if not slug:
        return redirect(url_for("index"))
    return redirect(url_for("complex_view", slug=slug) + "?tour=editor")


# ── Админка: мониторинг ──────────────────────────────────────────

@app.route("/admin/stats")
@admin_required
def admin_stats():
    days = min(365, max(7, request.args.get("days", 30, type=int)))
    return render_template("admin_stats.html",
                           data=_monitoring.dashboard_data(days),
                           user=g.get("user"))


@app.route("/admin/stats.json")
@admin_required
def admin_stats_json():
    """Те же данные машиночитаемо — для выгрузки или внешнего графика."""
    days = min(365, max(7, request.args.get("days", 30, type=int)))
    return jsonify(_monitoring.dashboard_data(days))


@app.route("/complex/new")
def complex_new():
    back = request.args.get('back', '')
    from_page = back if (back and back.startswith('/')) else '/'
    return render_template("complex_editor.html",
                           complex_id="null",
                           readonly=False,
                           from_page=from_page,
                           user=g.get("user"))


@app.route("/complex/<slug>/edit")
def complex_edit(slug: str):
    """Редактирование комплекса по UUID-слагу.
    Только владелец может редактировать; ghost-комплексы и чужие — перенаправляются на view.
    """
    try:
        _uuid.UUID(slug)
    except ValueError:
        abort(404)
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT id, user_id, visibility, is_ghost FROM complexes WHERE slug = %s",
                (slug,)
            )
            row = cur.fetchone()
    if not row:
        abort(404)
    complex_id, owner_id, visibility, is_ghost_complex = row

    current_uid = g.user["id"] if g.get("user") else None
    # Ghost-комплексы всегда read-only для всех.
    # Редактировать может только владелец (или анонимный пользователь свой guest-комплекс
    # если у него нет owner, но только если это не ghost).
    can_edit = (not is_ghost_complex) and (owner_id is None or current_uid == owner_id)
    if not can_edit:
        back = request.args.get('back', '')
        view_url = url_for("complex_view", slug=slug)
        if back and back.startswith('/'):
            view_url += f"?back={back}"
        return redirect(view_url)

    back = request.args.get('back', '')
    # Разрешаем только относительные URL (начинаются с /) — защита от open redirect
    if back and back.startswith('/'):
        from_page = back
    elif request.args.get('from') == 'community':
        from_page = '/?tab=community'
    else:
        from_page = '/'
    return render_template("complex_editor.html",
                           complex_id=complex_id,
                           readonly=False,
                           from_page=from_page,
                           user=g.get("user"))


@app.route("/complex/<slug>/view")
def complex_view(slug: str):
    """Просмотр комплекса (read-only).
    Если пользователь — владелец (и комплекс не ghost), перенаправляет на edit.
    """
    try:
        _uuid.UUID(slug)
    except ValueError:
        abort(404)
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("""
                SELECT c.id, c.user_id, c.visibility, c.is_ghost,
                       c.name, u.display_name AS author_name
                FROM complexes c
                LEFT JOIN users u ON u.id = c.user_id
                WHERE c.slug = %s
            """, (slug,))
            row = cur.fetchone()
    if not row:
        abort(404)
    complex_id, owner_id, visibility, is_ghost_complex, cx_name, cx_author = row

    current_uid = g.user["id"] if g.get("user") else None
    # Если текущий пользователь — владелец (и это не ghost) → отправляем сразу на редактирование
    if not is_ghost_complex and owner_id is not None and current_uid == owner_id:
        back = request.args.get('back', '')
        edit_url = url_for("complex_edit", slug=slug)
        if back and back.startswith('/'):
            edit_url += f"?back={back}"
        return redirect(edit_url)

    back = request.args.get('back', '')
    if back and back.startswith('/'):
        from_page = back
    elif request.args.get('from') == 'community':
        from_page = '/?tab=community'
    else:
        from_page = '/'
    return render_template("complex_editor.html",
                           complex_id=complex_id,
                           readonly=True,
                           from_page=from_page,
                           user=g.get("user"),
                           og_complex_name=cx_name,
                           og_author_name=cx_author)


# ─────────────────────────────────────────────────────────────────
# API: рецепты / комплексы
# ─────────────────────────────────────────────────────────────────

def _mnt_priority(maintenance: list) -> float:
    """Приоритет сортировки по обслуживанию — зеркало JS-логики."""
    if not maintenance:
        return 0.0
    def tier(s):
        m = _MNT_TIER_RE.search(s)
        return int(m.group(1)) if m else 1
    top = max(maintenance, key=lambda e: tier(e.get('item', '')))
    return tier(top.get('item', '')) * 100000 + float(top.get('rate_per_min') or 0)


@app.route("/api/nodes")
def api_nodes():
    q_raw       = request.args.get("q",      "").strip()
    type_filter = request.args.get("type",   "all")
    show_hidden = request.args.get("hidden", "false") == "true"
    sort_key    = request.args.get("sort",   "")          # name|workers|electricity|maintenance
    sort_dir    = request.args.get("dir",    "asc")       # asc|desc
    page        = max(1, int(request.args.get("page", "1")))
    per_page    = min(100, max(10, int(request.args.get("per_page", "50"))))
    search_ast  = _parse_search(q_raw)

    hidden_ids:         set[int] = _get_hidden_recipe_ids(g.user["id"])
    hidden_complex_ids: set[int] = _get_hidden_complex_ids(g.user["id"])

    nodes_sql, nodes_params = _make_nodes_sql(g.user["id"])
    try:
        with get_db() as con:
            with dict_cursor(con) as cur:
                cur.execute(nodes_sql, nodes_params)
                rows = [_parse_row(r) for r in cur.fetchall()]
    except Exception as e:
        return _err500(e)

    result = []
    for row in rows:
        if type_filter != "all" and row["node_type"] != type_filter:
            continue
        if row["node_type"] == "recipe":
            row["deprecated"] = row["node_id"] in hidden_ids
        elif row["node_type"] == "complex":
            row["deprecated"] = row["node_id"] in hidden_complex_ids
        if not show_hidden and row["deprecated"]:
            continue
        if search_ast and not _eval_search(search_ast, row):
            continue
        result.append(row)

    # ── Сортировка всей выборки перед пагинацией ─────────────────────
    if sort_key:
        rev = (sort_dir == "desc")
        if sort_key == "name":
            result.sort(
                key=lambda r: (r.get("machine_name") or r.get("name") or "").casefold(),
                reverse=rev,
            )
        elif sort_key == "workers":
            result.sort(key=lambda r: max(0, r.get("workers") or 0), reverse=rev)
        elif sort_key == "electricity":
            result.sort(key=lambda r: abs(r.get("electricity_kw") or 0), reverse=rev)
        elif sort_key == "maintenance":
            result.sort(key=lambda r: _mnt_priority(r.get("maintenance") or []), reverse=rev)
        elif sort_key == "computing":
            result.sort(key=lambda r: r.get("computing_tf") or 0, reverse=rev)

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
                SELECT json_agg(json_build_object('item', cmnt.item, 'rate_per_min', cmnt.rate_per_min) ORDER BY cmnt.item) AS items
                FROM complex_maintenance cmnt
                WHERE cmnt.complex_id = c.id
            ) mnt ON TRUE
            WHERE c.visibility = 'public' AND c.is_ghost = FALSE
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
            return _err500(e)

        result = []
        for r in rows:
            row = _parse_row(r)
            row["author_name"]  = r.get("author_name")
            row["likes_count"]  = r.get("likes_count", 0)
            row["is_community"] = True
            result.append(row)
        return jsonify(result)

    # ── Обычный режим: свои рецепты + свои комплексы + подписки ──
    hidden_ids:         set[int] = _get_hidden_recipe_ids(g.user["id"])
    hidden_complex_ids: set[int] = _get_hidden_complex_ids(g.user["id"])

    nodes_sql, nodes_params = _make_nodes_sql(g.user["id"])
    try:
        with get_db() as con:
            with dict_cursor(con) as cur:
                cur.execute(nodes_sql, nodes_params)
                rows = [_parse_row(r) for r in cur.fetchall()]
    except Exception as e:
        return _err500(e)

    result = []
    for row in rows:
        if row["node_type"] == "recipe":
            row["_hidden"] = row["node_id"] in hidden_ids
        elif row["node_type"] == "complex":
            row["_hidden"] = row["node_id"] in hidden_complex_ids
        if not show_hid:
            if row["node_type"] == "recipe" and row["node_id"] in hidden_ids:
                continue
            if row["node_type"] == "complex" and row["node_id"] in hidden_complex_ids:
                continue
        if type_flt != "all" and row["node_type"] != type_flt:
            continue
        check = row["outputs"] if direction == "produces" else row["inputs"]
        if any((x.get("item_en") or x["item"]) == item for x in check):
            result.append(row)

    return jsonify(result)


@app.route("/api/node/<node_type>/<int:node_id>")
def api_node_detail(node_type: str, node_id: int):
    if node_type not in ("recipe", "complex"):
        return jsonify({"error": "invalid type"}), 400
    nodes_sql, nodes_params = _make_nodes_sql(g.user["id"])
    try:
        with get_db() as con:
            with dict_cursor(con) as cur:
                cur.execute(nodes_sql, nodes_params)
                rows = [_parse_row(r) for r in cur.fetchall()]
        row = next(
            (r for r in rows if r["node_type"] == node_type and r["node_id"] == node_id),
            None,
        )
        if not row:
            return jsonify({"error": "not found"}), 404
        return jsonify(row)
    except Exception as e:
        return _err500(e)


# ─────────────────────────────────────────────────────────────────
# API: расчёт нужд поселения
# ─────────────────────────────────────────────────────────────────

@app.route("/api/population/data")
def api_population_data():
    """Потребности населения вместе с рецептами-кандидатами.

    Отдаём всё одним запросом: страница пересчитывается в браузере при каждом
    изменении параметров, ходить на сервер за этим не нужно.

    Кандидаты подбираются по типу потребности:
      need_type=0 (потребление) — рецепты, которые предмет производят;
      need_type=1 (отход)       — рецепты, которые его потребляют;
      need_type=2 (нагрузка)    — тоже производители: электроэнергия и
                                  вычисления в игре обычные предметы.
    """
    trans = getattr(g, "content_trans", {})

    def disp(name):
        return trans.get(name, name) if name else name

    HIDDEN_GROUPS: tuple[str, ...] = ()

    # Компоненты медикаментов делаются только для них, поэтому цепочку
    # разворачиваем: сами уровни лекарств — рекурсивно, их составляющие —
    # на один шаг. Глубже не идём, дальше уже общепромышленное сырьё.
    MEDICINE_ITEMS = {"Medical Supplies", "Medical Supplies II", "Medical Supplies III"}

    # Охлаждённая вода делается единственным рецептом и только для ЦОД,
    # поэтому её производство тоже показываем цепочкой.
    DEDICATED_INPUTS = {"Computing": ["Chilled Water"]}

    # Пищевой подграф: обход от потребностей питания вниз, пока предмет
    # делается на пищевом здании. Мак и сахар добавлены корнями явно —
    # они нужны медикаментам (морфий и антибиотики), но сами растут и
    # обрабатываются в пищевой цепочке.
    FOOD_BUILDINGS = ("Farm", "Irrigated Farm", "Greenhouse", "Greenhouse II",
                      "Chicken Farm", "Food Processor", "Mill", "Baking Unit",
                      "Mixer", "Mixer II")
    CHAIN_EXTRA_ROOTS = ("Poppy", "Sugar", "Food Pack")
    # Пайки делают на сборочной линии, а не на пищевом здании, но по сути
    # это такой же продукт цепочки: он потребляет готовую еду.
    CHAIN_ANY_BUILDING = ("Food Pack",)
    # Вода и удобрения — общие ресурсы, вглубь не разворачиваются.
    def chain_stop(name):
        return name == "Water" or name.startswith("Fertilizer")

    # Воду в игре «производят» многие рецепты — от градирен до крекинга,
    # но там она побочный выход. Для покрытия потребности поселения годятся
    # только те здания, которые её именно добывают.
    WATER_PRODUCERS = ("Groundwater Pump", "Rainwater Harvester", "Thermal Desalinator")

    # Фермы выбираются одним переключателем на всё поселение, а не в каждой
    # строке: иначе комбинаторика раскладки культур по фермам станет
    # неуправляемой. Плантация работает без удобрений, остальные три — с ними.
    FARM_BUILDINGS = ("Farm", "Irrigated Farm", "Greenhouse", "Greenhouse II")

    # Куда девать то, что план вырастил, но не израсходовал. Порядок важен:
    # сначала газ, потом компост, и только если больше некуда — горелка.
    # Ферментёр принимает восемь культур из девяти, рапс — только в горелку.
    DISPOSAL_BUILDINGS = ("Anaerobic Digester", "Mixer", "Mixer II",
                          "Shredder", "Food Processor", "Burner (Solid)")
    # «process» — не утилизация, а передел: лишние тушки идут в потроха,
    # а те уже в газ. Поэтому он первым в очереди способов.
    DISPOSAL_ORDER = {"process": 0, "gas": 1, "biomass": 2, "compost": 3, "burn": 4}

    def disposal_kind(outputs):
        """Что это за переработка — по тому, что выходит из рецепта."""
        names = {o["item"] for o in outputs}
        if "Fuel Gas" in names:
            return "gas"
        if "Compost" in names:
            return "compost"
        # Саженцы девать некуда, кроме шредера, а биомассу из него уже
        # примет смеситель — переработка в два шага.
        if names == {"Biomass"}:
            return "biomass"
        if names == {"Meat Trimmings"}:
            return "process"
        if names and names <= {"Air Pollution"}:
            return "burn"
        return None

    # Выбор по умолчанию: (предмет, здание, обязательный вход или None).
    PREFERRED = [
        ("Waste Water",  "Wastewater Treatment",   "Filter Media"),
        ("Waste",        "Incineration Plant",     "Hydrogen"),
        ("Electricity",  "Power Generator (Large)", None),
        ("Computing",    "Data Center",             None),
    ]

    # Рецепты младших уровней помечены deprecated («вытеснено старшим
    # уровнем»), а не «убрано из игры»: 198 из 206 таких — ровно варианты
    # уровней. Пропускаем их, если у здания есть старшие уровни.
    TIER_CTE = """
        WITH fam AS (
            SELECT id, name, regexp_replace(name, ' (I{1,3}|IV|V|VI)$', '') AS base
            FROM buildings
        ),
        multi AS (SELECT base FROM fam GROUP BY base HAVING count(*) > 1)
    """

    try:
        with get_db() as con:
            with dict_cursor(con) as cur:
                cur.execute("""
                    SELECT n.id, n.item, n.label, n.need_type, n.qty_per_10k, n.unit,
                           n.group_key, n.choice_group, n.is_default, n.food_group,
                           n.sort_order,
                           -- Сколько всего продуктов в группе питания: норма в
                           -- qty_per_10k задана для полного состава, и из этого
                           -- числа выводится «полная» норма продукта.
                           (SELECT count(*) FROM population_needs m
                             WHERE m.food_group = n.food_group) AS group_size
                    FROM population_needs n
                    WHERE NOT (n.group_key = ANY(%s))
                    ORDER BY n.sort_order
                """, (list(HIDDEN_GROUPS),))
                needs = [dict(r) for r in cur.fetchall()]

                # ── Кандидаты для самих потребностей ─────────────────
                cur.execute(TIER_CTE + """
                    SELECT n.id           AS need_id,
                           r.id           AS recipe_id,
                           r.cycle_time_s,
                           b.id           AS building_id,
                           b.name         AS building,
                           f.base         AS family,
                           b.workers,
                           b.electricity_kw,
                           b.computing_tf,
                           rf.qty_per_min AS rate
                    FROM population_needs n
                    JOIN items i           ON i.name = n.item
                    JOIN resource_flows rf ON rf.item_id = i.id
                                          AND rf.parent_type = 0
                                          AND rf.direction = CASE n.need_type
                                                                  WHEN 1 THEN 0
                                                                  ELSE 1
                                                             END
                    JOIN recipes r         ON r.id = rf.recipe_id
                    LEFT JOIN buildings b  ON b.id = r.machine_id
                    LEFT JOIN fam f        ON f.id = b.id
                    WHERE NOT (n.group_key = ANY(%s))
                      AND (NOT r.deprecated OR f.base IN (SELECT base FROM multi))
                    ORDER BY n.id, rf.qty_per_min DESC
                """, (list(HIDDEN_GROUPS),))
                cand_rows = [dict(r) for r in cur.fetchall()]

                # ── Производители произвольных предметов (для дерева) ─
                cur.execute(TIER_CTE + """
                    SELECT i.name         AS produces,
                           r.id           AS recipe_id,
                           r.cycle_time_s,
                           b.id           AS building_id,
                           b.name         AS building,
                           f.base         AS family,
                           b.workers,
                           b.electricity_kw,
                           b.computing_tf,
                           rf.qty_per_min AS rate
                    FROM resource_flows rf
                    JOIN items i          ON i.id = rf.item_id
                    JOIN recipes r        ON r.id = rf.recipe_id
                    LEFT JOIN buildings b ON b.id = r.machine_id
                    LEFT JOIN fam f       ON f.id = b.id
                    WHERE rf.parent_type = 0 AND rf.direction = 1
                      AND (NOT r.deprecated OR f.base IN (SELECT base FROM multi))
                    ORDER BY i.name, rf.qty_per_min DESC
                """)
                producer_rows: dict[str, list] = {}
                for r in cur.fetchall():
                    producer_rows.setdefault(r["produces"], []).append(dict(r))

                # ── Переработка: кто съедает предмет насовсем ────────
                cur.execute(TIER_CTE + """
                    SELECT i.name         AS consumes,
                           r.id           AS recipe_id,
                           r.cycle_time_s,
                           b.id           AS building_id,
                           b.name         AS building,
                           f.base         AS family,
                           b.workers,
                           b.electricity_kw,
                           b.computing_tf,
                           rf.qty_per_min AS rate
                    FROM resource_flows rf
                    JOIN items i          ON i.id = rf.item_id
                    JOIN recipes r        ON r.id = rf.recipe_id
                    JOIN buildings b      ON b.id = r.machine_id
                    LEFT JOIN fam f       ON f.id = b.id
                    WHERE rf.parent_type = 0 AND rf.direction = 0
                      AND b.name = ANY(%s)
                      AND (NOT r.deprecated OR f.base IN (SELECT base FROM multi))
                    ORDER BY i.name, rf.qty_per_min DESC
                """, (list(DISPOSAL_BUILDINGS),))
                disposal_rows = [dict(r) for r in cur.fetchall()]

                # ── Составы рецептов ────────────────────────────────
                cur.execute("""
                    SELECT rf.recipe_id, rf.direction, i.name AS item,
                           rf.qty_per_min, rf.sort_order
                    FROM resource_flows rf
                    JOIN items i ON i.id = rf.item_id
                    WHERE rf.parent_type = 0
                    ORDER BY rf.recipe_id, rf.direction, rf.sort_order
                """)
                flows_by_recipe: dict[int, dict[str, list]] = {}
                for f in cur.fetchall():
                    slot = flows_by_recipe.setdefault(
                        f["recipe_id"], {"inputs": [], "outputs": []}
                    )
                    key = "outputs" if f["direction"] == 1 else "inputs"
                    slot[key].append({
                        "item":         f["item"],
                        "item_display": disp(f["item"]),
                        "qty_per_min":  float(f["qty_per_min"] or 0),
                        # У выходов нулевой sort_order стоит у основного
                        # продукта: по нему и считается размер рецепта,
                        # остальные выходы дают лишь зачёт.
                        "primary":      f["direction"] == 1 and f["sort_order"] == 0,
                    })

                # ── Обслуживание (запчасти) по зданиям ──────────────
                cur.execute("SELECT building_id, item, rate_per_min FROM building_maintenance")
                mnt_by_building: dict[int, list] = {}
                for m in cur.fetchall():
                    mnt_by_building.setdefault(m["building_id"], []).append({
                        "item":         m["item"],
                        "item_display": disp(m["item"]),
                        "rate_per_min": float(m["rate_per_min"] or 0),
                    })

                # ── Семейства зданий с уровнями ─────────────────────
                cur.execute("""
                    SELECT regexp_replace(name, ' (I{1,3}|IV|V|VI)$', '') AS base,
                           id, name
                    FROM buildings
                    ORDER BY 1, 3
                """)
                fam_members: dict[str, list] = {}
                for b in cur.fetchall():
                    fam_members.setdefault(b["base"], []).append(b["name"])

                # ── Выращиваемые предметы: всё, что выходит с ферм ──
                cur.execute("""
                    SELECT DISTINCT i.name
                    FROM resource_flows rf
                    JOIN items i     ON i.id = rf.item_id
                    JOIN recipes r   ON r.id = rf.recipe_id AND NOT r.deprecated
                    JOIN buildings b ON b.id = r.machine_id
                    WHERE rf.parent_type = 0 AND rf.direction = 1
                      AND b.name IN ('Farm', 'Irrigated Farm', 'Greenhouse',
                                     'Greenhouse II', 'Chicken Farm')
                """)
                grown_items = sorted(r["name"] for r in cur.fetchall())
    except Exception as e:
        return _err500(e)

    ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6}

    def tier_of(name):
        if not name:
            return 1
        return ROMAN.get(name.rsplit(" ", 1)[-1], 1)

    def make_option(row, rate, produces=None):
        flows = flows_by_recipe.get(row["recipe_id"], {"inputs": [], "outputs": []})
        row = dict(row, produces_item=produces or row.get("produces"))
        return {
            "recipe_id":        row["recipe_id"],
            "building":         row["building"],
            "building_display": disp(row["building"]),
            "family":           row.get("family") or row["building"],
            "tier":             tier_of(row["building"]),
            "cycle_time_s":     float(row.get("cycle_time_s") or 0),
            "workers":          float(row["workers"] or 0),
            "electricity_kw":   float(row["electricity_kw"] or 0),
            "computing_tf":     float(row["computing_tf"] or 0),
            "maintenance":      mnt_by_building.get(row["building_id"], []),
            "rate":             float(rate or 0),
            "inputs":           flows["inputs"],
            "outputs":          flows["outputs"],
            # Основной ли выход у этого варианта: если нет, предмет приходит
            # побочно и такой вариант нельзя выбрать как способ производства.
            "is_primary":       any(o["primary"] and o["item"] == row.get("produces_item")
                                    for o in flows["outputs"]),
            # «Род» рецепта — по сырью, без учёта здания: смеситель I и II
            # с кукурузой это один и тот же рецепт на разных уровнях.
            "kind":             " + ".join(sorted(
                                    i["item"] for i in flows["inputs"]
                                    if i["item"] != "Water")) or "—",
            "kind_display":     ", ".join(sorted(
                                    (disp(i["item"]) for i in flows["inputs"]
                                     if i["item"] != "Water"),
                                    key=str.casefold)) or "—",
            # Для фермовых рецептов — на какой ферме и с каким удобрением.
            "farm":             row["building"] if row["building"] in FARM_BUILDINGS else None,
            "fertilizer":       next((i["item"] for i in flows["inputs"]
                                      if i["item"].startswith("Fertilizer")), None),
        }

    def sort_options(opts):
        # Уровень в ключ не входит: между разными семействами он несравним
        # (Diesel Generator II — второй уровень, но втрое слабее одноуровневого
        # Power Generator (Large)). Внутри семейства старший уровень и так быстрее.
        opts.sort(key=lambda o: (len(o["inputs"]), -o["rate"]))
        return opts

    def options_for(item):
        return sort_options([make_option(r, r["rate"]) for r in producer_rows.get(item, [])])

    def mark_preferred(item, opts):
        """Проставляет выбор по умолчанию по правилу, а не по id рецепта:
        id меняются при переимпорте данных, названия — нет."""
        for want_item, want_building, want_input in PREFERRED:
            if want_item != item:
                continue
            for o in opts:
                if o["building"] != want_building:
                    continue
                if want_input and not any(i["item"] == want_input for i in o["inputs"]):
                    continue
                o["preferred"] = True
                return
        return

    # ── Кандидаты по потребностям ────────────────────────────────────
    options_by_need: dict[int, list] = {}
    needs_by_id = {n["id"]: n for n in needs}
    for c in cand_rows:
        need = needs_by_id.get(c["need_id"])
        if not need:
            continue
        opt = make_option(c, c["rate"], produces=need["item"])
        # Отход считается переработанным, только если он действительно исчезает.
        # Пресс превращает Waste в Waste Pressed — это перекладывание.
        if need["need_type"] == 1 and any(
            o["item"] != need["item"] and o["item"].startswith(need["item"])
            for o in opt["outputs"]
        ):
            continue
        options_by_need.setdefault(c["need_id"], []).append(opt)

    for need_id, opts in options_by_need.items():
        if needs_by_id[need_id]["item"] == "Water":
            opts[:] = [o for o in opts if o["building"] in WATER_PRODUCERS]
        sort_options(opts)
        mark_preferred(needs_by_id[need_id]["item"], opts)

    # ── Дерево производства медикаментов ────────────────────────────
    def medicine_tree(root_item):
        """Уровни лекарств разворачиваем рекурсивно, их составляющие — на шаг.

        Глубже не идём: там начинается общепромышленное сырьё, которое
        используется не только для медикаментов.

        Порядок — обход в глубину: поддерево узла идёт сразу под ним, иначе
        в лесенке антибиотики оказались бы не под медикаментами II, а после
        анестетиков и морфия.
        """
        seen = {root_item}

        def walk(cur_item):
            out = []
            src = producer_rows.get(cur_item)
            if not src:
                return out
            for inp in flows_by_recipe.get(src[0]["recipe_id"], {}).get("inputs", []):
                name = inp["item"]
                if name in seen or name not in producer_rows:
                    continue
                seen.add(name)
                opts = options_for(name)
                mark_preferred(name, opts)
                out.append({
                    "item":         name,
                    "item_display": disp(name),
                    "parent":       cur_item,
                    "is_medicine":  name in MEDICINE_ITEMS,
                    "options":      opts,
                })
                if name in MEDICINE_ITEMS:
                    out.extend(walk(name))
            return out

        return walk(root_item)

    def dedicated_tree(root_item):
        """Узлы для входов, которые делаются только ради этой цепочки."""
        nodes = []
        for name in DEDICATED_INPUTS.get(root_item, []):
            if name not in producer_rows:
                continue
            opts = options_for(name)
            mark_preferred(name, opts)
            nodes.append({
                "item":         name,
                "item_display": disp(name),
                "parent":       root_item,
                "is_medicine":  False,
                "options":      opts,
            })
        return nodes

    result = []
    for n in needs:
        opts = options_by_need.get(n["id"], [])
        result.append({
            "id":           n["id"],
            "item":         n["item"],
            "item_display": disp(n["item"]),
            "label":        n["label"],
            "need_type":    n["need_type"],
            "qty_per_10k":  float(n["qty_per_10k"]),
            "unit":         n["unit"],
            "group_key":    n["group_key"],
            "choice_group": n["choice_group"],
            "is_default":   n["is_default"],
            "food_group":   n["food_group"],
            "group_size":   int(n["group_size"] or 0),
            # Вода, электроэнергия и вычисления потребляются не только
            # населением, поэтому их потребность собирается на странице
            # из всех источников, а не берётся из нормы напрямую.
            "shared":       n["item"] in ("Water", "Electricity", "Computing"),
            "options":      opts,
            "tree":         (medicine_tree(n["item"]) if n["item"] in MEDICINE_ITEMS
                             else dedicated_tree(n["item"])),
        })

    # ── Пищевой подграф ─────────────────────────────────────────────
    def food_options(item):
        """Производители предмета внутри пищевой цепочки."""
        out = []
        for r in producer_rows.get(item, []):
            if r["building"] not in FOOD_BUILDINGS and item not in CHAIN_ANY_BUILDING:
                continue
            out.append(make_option(r, r["rate"], produces=item))
        return sort_options(out)

    chain: dict[str, dict] = {}
    roots = [n["item"] for n in needs if n["group_key"] == "food" and n["item"]]
    roots += list(CHAIN_EXTRA_ROOTS)
    queue, seen_c = list(roots), set(roots)
    while queue:
        cur_item = queue.pop(0)
        opts = food_options(cur_item)
        if not opts:
            continue
        chain[cur_item] = {
            "item":         cur_item,
            "item_display": disp(cur_item),
            "options":      opts,
            # Культура — то, что растёт на ферме; такие строки на последнем
            # этапе будут раскладываться по полям.
            "is_crop":      any(o["farm"] for o in opts),
        }
        for o in opts:
            for i in o["inputs"]:
                if chain_stop(i["item"]) or i["item"] in seen_c:
                    continue
                if any(r["building"] in FOOD_BUILDINGS
                       for r in producer_rows.get(i["item"], [])):
                    seen_c.add(i["item"])
                    queue.append(i["item"])
    for c in chain.values():
        mark_preferred(c["item"], c["options"])
    # У пайков по умолчанию работают оба куриных рецепта: птицефабрика даёт
    # яйца и тушки разом, и брать только одно — значит выбрасывать другое.
    # Тофу с овощами остаётся на усмотрение — его включают галочкой.
    for o in chain.get("Food Pack", {}).get("options", []):
        if any(i["item"] in ("Eggs", "Meat") for i in o["inputs"]):
            o["preferred"] = True

    # ── Переработка излишков ────────────────────────────────────────
    # Предмет -> способы от него избавиться, от полезного к бесполезному.
    # Годится только рецепт из одного входа: смеситель делает из кукурузы
    # корм, но это производство, а не утилизация.
    disposal: dict[str, list] = {}
    for r in disposal_rows:
        flows = flows_by_recipe.get(r["recipe_id"], {"inputs": [], "outputs": []})
        if len(flows["inputs"]) != 1:
            continue
        kind = disposal_kind(flows["outputs"])
        if not kind:
            continue
        o = make_option(r, r["rate"], produces=r["consumes"])
        o["disposal"] = kind
        disposal.setdefault(r["consumes"], []).append(o)
    for item, opts in disposal.items():
        # Порядок: сначала полезные способы, внутри способа — быстрые.
        # Уровень здания выбирается на странице, поэтому оставляем все.
        opts.sort(key=lambda o: (DISPOSAL_ORDER[o["disposal"]], -o["rate"]))

    # ── Семейства, для которых уровень действительно решает ─────────
    # Переключатель уровня имеет смысл там, где уровень — единственная ось
    # выбора: хозтовары делает только семейство Assembly, анестетики — только
    # Chemical Plant. А электроэнергию, кроме дизель-генераторов, дают ещё и
    # обычные электрогенераторы, поэтому там выбирают рецептом, а не уровнем.
    used_families: dict[str, set] = {}
    def collect(opts):
        # Побочные варианты в расчёт не берём: корм приходит и с мельницы,
        # но выбирают его всё равно на смесителе. Иначе смеситель не попал бы
        # в список уровней только из-за побочных источников.
        opts = [o for o in opts if o["is_primary"]]
        if not opts:
            return
        # Фермы настраиваются отдельной парой «ферма + удобрение», поэтому
        # в список уровней зданий не попадают — иначе теплица оказалась бы
        # сразу в двух местах.
        if any(o["farm"] for o in opts):
            return
        fams = {o["family"] for o in opts}
        if len(fams) != 1:
            return
        fam = fams.pop()
        if len(fam_members.get(fam, [])) > 1:
            used_families.setdefault(fam, set()).update(o["building"] for o in opts)
    for n in result:
        collect(n["options"])
        for node in n["tree"]:
            collect(node["options"])
    for c in chain.values():
        collect(c["options"])

    families = []
    for base, buildings in sorted(used_families.items()):
        tiers = sorted(
            ({"tier": tier_of(b), "building": b, "building_display": disp(b)}
             for b in fam_members[base]),
            key=lambda x: x["tier"],
        )
        # Базовое имя семейства («Assembly») зданием не является, поэтому
        # переводить нечего. Берём название старшего уровня и снимаем цифру:
        # «Сборочная линия V» -> «Сборочная линия».
        top = tiers[-1]["building_display"]
        display = re.sub(r"\s+(I{1,3}|IV|V|VI)$", "", top)
        families.append({
            "family":  base,
            "display": display or disp(base),
            "tiers":   tiers,
            "max":     max(t["tier"] for t in tiers),
        })

    prefs = {}
    if g.get("user"):
        raw = _get_user_setting(g.user["id"], "population_prefs")
        if raw:
            try:
                prefs = json.loads(raw)
            except Exception:
                prefs = {}

    # Справочник ферм: какие удобрения на каждой вообще применимы.
    farms = []
    for fb in FARM_BUILDINGS:
        ferts, seen_f = [], set()
        for rows in producer_rows.values():
            for r in rows:
                if r["building"] != fb:
                    continue
                f = next((i["item"] for i in
                          flows_by_recipe.get(r["recipe_id"], {}).get("inputs", [])
                          if i["item"].startswith("Fertilizer")), None)
                if f not in seen_f:
                    seen_f.add(f)
                    ferts.append({"item": f, "display": disp(f) if f else None})
        if not seen_f:
            continue
        ferts.sort(key=lambda x: (x["item"] is not None, x["item"] or ""))
        farms.append({"building": fb, "display": disp(fb), "fertilizers": ferts})

    # ── Удобрения ───────────────────────────────────────────────────
    # Что именно нужно, решает выбор удобрения слева. Цепочка короткая:
    # второе делается из первого, первое — с органическим или без него,
    # органическое — из компоста, который план и так производит.
    fert = {}
    for it in ("Fertilizer II", "Fertilizer I", "Fertilizer (Organic)"):
        opts = sort_options([make_option(r, r["rate"], produces=it)
                             for r in producer_rows.get(it, [])])
        if not opts:
            continue
        # По умолчанию органическое делаем с дёрном: компоста уходит втрое
        # меньше, а дёрн копают экскаватором и девать его всё равно некуда.
        if it == "Fertilizer (Organic)":
            for o in opts:
                if any(i["item"] == "Dirt" for i in o["inputs"]):
                    o["preferred"] = True
        fert[it] = {"item": it, "item_display": disp(it), "options": opts}

    sapling = None
    sap_opts = sort_options([make_option(r, r["rate"], produces="Tree Sapling")
                             for r in producer_rows.get("Tree Sapling", [])])
    if sap_opts:
        sapling = {"item": "Tree Sapling", "item_display": disp("Tree Sapling"),
                   "options": sap_opts}

    return jsonify({
        "fert":            fert,
        "sapling":         sapling,
        "needs":           result,
        "families":        families,
        "farms":           farms,
        "chain":           chain,
        "disposal":        disposal,
        "base_population": 10000,
        "grown_items":     grown_items,
        "prefs":           prefs,
    })


# ─────────────────────────────────────────────────────────────────
# API: комплексы (публичные / мои / форк)
# ─────────────────────────────────────────────────────────────────

@app.route("/api/complexes/public")
def api_public_complexes():
    """Список публичных комплексов с пагинацией и сортировкой."""
    sort    = request.args.get("sort", "new")   # new | popular
    page    = max(1, int(request.args.get("page", "1")))
    per_page = min(50, max(5, int(request.args.get("per_page", "20"))))
    q       = request.args.get("q", "").strip()

    order = "c.likes_count DESC, c.id DESC" if sort == "popular" else "c.id DESC"
    offset = (page - 1) * per_page

    viewer_id = g.user["id"] if g.get("user") else None

    # Используем тот же парсер, что и browse (in:/out:/name:/by: + &|())
    trans     = getattr(g, "content_trans", {})
    trans_rev = {v.lower(): k for k, v in trans.items()} if trans else {}

    ast = _parse_search(q) if q.strip() else None
    filter_params: list = []
    where_frag, needs_users = _build_community_where(ast, filter_params, trans_rev)
    count_params = list(filter_params)

    extra_where = f" AND {where_frag}" if ast else ""
    count_join  = "LEFT JOIN users u ON u.id = c.user_id" if needs_users else ""

    with get_db() as con:
        with dict_cursor(con) as cur:
            cur.execute(f"""
                SELECT COUNT(*) AS total
                FROM complexes c
                {count_join}
                WHERE c.visibility = 'public' AND c.is_ghost = FALSE
                {extra_where}
            """, count_params)
            total = cur.fetchone()["total"]

            cur.execute(f"""
                SELECT c.id, c.slug, c.name, c.description, c.likes_count,
                       c.total_workers, c.total_electricity_kw,
                       c.user_id,
                       u.display_name AS author, u.avatar_url AS author_avatar,
                       c.forked_from_id,
                       cf.slug AS forked_from_slug,
                       c.updated_at,
                       (cl.complex_id IS NOT NULL) AS _liked,
                       (cs.complex_id IS NOT NULL) AS _subscribed,
                       (SELECT COUNT(*) FROM complexes c2
                        WHERE c2.forked_from_id = c.id)::int AS fork_count,
                       (SELECT json_agg(json_build_object(
                                            'item', i.name,
                                            'qty_per_min', rf.qty_per_min)
                                        ORDER BY rf.sort_order)
                        FROM resource_flows rf JOIN items i ON i.id = rf.item_id
                        WHERE rf.parent_type = 1 AND rf.parent_id = c.id
                          AND rf.direction = 0) AS inputs,
                       (SELECT json_agg(json_build_object(
                                            'item', i.name,
                                            'qty_per_min', rf.qty_per_min)
                                        ORDER BY rf.sort_order)
                        FROM resource_flows rf JOIN items i ON i.id = rf.item_id
                        WHERE rf.parent_type = 1 AND rf.parent_id = c.id
                          AND rf.direction = 1) AS outputs,
                       (SELECT json_agg(json_build_object(
                                            'item', cm2.item,
                                            'rate_per_min', cm2.rate_per_min))
                        FROM complex_maintenance cm2
                        WHERE cm2.complex_id = c.id) AS maintenance,
                       (SELECT json_agg(json_build_object('item', cc.item, 'qty', cc.qty) ORDER BY cc.item)
                        FROM complex_construction cc
                        WHERE cc.complex_id = c.id) AS construction,
                       COALESCE((SELECT json_agg(tg.name ORDER BY tg.name)
                        FROM complex_tags ct2
                        JOIN tags tg ON tg.id = ct2.tag_id
                        WHERE ct2.complex_id = c.id), '[]'::json) AS tags
                FROM complexes c
                LEFT JOIN users u ON u.id = c.user_id
                LEFT JOIN complex_likes cl
                       ON cl.complex_id = c.id AND cl.user_id = %s
                LEFT JOIN complex_subscriptions cs
                       ON cs.complex_id = c.id AND cs.user_id = %s
                LEFT JOIN complexes cf ON cf.id = c.forked_from_id
                WHERE c.visibility = 'public' AND c.is_ghost = FALSE
                {extra_where}
                ORDER BY {order}
                LIMIT %s OFFSET %s
            """, [viewer_id, viewer_id] + filter_params + [per_page, offset])
            items = [dict(r) for r in cur.fetchall()]

    # Перевести названия ресурсов; сохранить item_en (EN-имя) для поиска иконок
    # trans уже получен выше при построении WHERE
    for row in items:
        # Конвертировать Decimal в float (total_electricity_kw может быть numeric)
        for num_col in ("total_electricity_kw", "total_workers", "likes_count"):
            if isinstance(row.get(num_col), decimal.Decimal):
                row[num_col] = float(row[num_col])
        # Always parse and default construction to []
        c_lst = row.get("construction")
        if isinstance(c_lst, str):
            try:
                c_lst = json.loads(c_lst)
            except Exception:
                c_lst = None
        row["construction"] = c_lst if isinstance(c_lst, list) else []

        if trans:
            for key in ("inputs", "outputs", "maintenance", "construction"):
                lst = row.get(key)
                if isinstance(lst, str):
                    try:
                        lst = json.loads(lst)
                    except Exception:
                        lst = None
                if isinstance(lst, list):
                    row[key] = [
                        {**r,
                         "item_en": r.get("item", ""),
                         "item":    trans.get(r.get("item", ""), r.get("item", ""))}
                        for r in lst if isinstance(r, dict)
                    ]

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
                SELECT c.id, c.slug, c.name, c.description, c.visibility,
                       c.likes_count, c.total_workers, c.total_electricity_kw,
                       c.forked_from_id, c.updated_at,
                       cf.slug AS forked_from_slug,
                       COALESCE((SELECT json_agg(tg.name ORDER BY tg.name)
                        FROM complex_tags ct2
                        JOIN tags tg ON tg.id = ct2.tag_id
                        WHERE ct2.complex_id = c.id), '[]'::json) AS tags
                FROM complexes c
                LEFT JOIN complexes cf ON cf.id = c.forked_from_id
                WHERE c.user_id = %s
                ORDER BY c.id DESC
            """, (g.user["id"],))
            items = [dict(r) for r in cur.fetchall()]

    return jsonify(items)


@app.route("/api/complex/<int:complex_id>/cascade-info")
def api_cascade_info(complex_id: int):
    """Вернуть вложенные и зависимые комплексы для предупреждений каскадных операций."""
    with get_db() as con:
        with dict_cursor(con) as cur:
            # Все дочерние комплексы рекурсивно (включены в этот комплекс)
            cur.execute("""
                WITH RECURSIVE children AS (
                    SELECT cm.child_complex_id AS id
                    FROM complex_members cm
                    WHERE cm.complex_id = %s AND cm.child_complex_id IS NOT NULL
                    UNION ALL
                    SELECT cm.child_complex_id
                    FROM complex_members cm
                    JOIN children c ON cm.complex_id = c.id
                    WHERE cm.child_complex_id IS NOT NULL
                )
                SELECT DISTINCT c2.id, c2.name, c2.visibility
                FROM children ch
                JOIN complexes c2 ON c2.id = ch.id
            """, (complex_id,))
            all_children = cur.fetchall()

            # Все родительские комплексы рекурсивно (используют этот комплекс)
            cur.execute("""
                WITH RECURSIVE parents AS (
                    SELECT cm.complex_id AS id
                    FROM complex_members cm
                    WHERE cm.child_complex_id = %s
                    UNION ALL
                    SELECT cm.complex_id
                    FROM complex_members cm
                    JOIN parents p ON cm.child_complex_id = p.id
                )
                SELECT DISTINCT c2.id, c2.name, c2.visibility
                FROM parents p
                JOIN complexes c2 ON c2.id = p.id
            """, (complex_id,))
            all_parents = cur.fetchall()

    return jsonify({
        "private_children": [x for x in all_children if x["visibility"] == "private"],
        "public_parents":   [x for x in all_parents  if x["visibility"] == "public"],
        "all_parents":      list(all_parents),
    })


# ─────────────────────────────────────────────────────────────────
# Shadow fork helper
# ─────────────────────────────────────────────────────────────────

def _create_shadow_fork(con, complex_id: int, reason: str):
    """Создаёт ghost-копию комплекса для зависимых комплексов других пользователей.

    Если другие пользователи используют complex_id как узел в своих комплексах,
    создаётся заморозка (ghost) текущего состояния и их complex_members переключаются
    на ghost, чтобы не сломать их схемы при редактировании/удалении/приватизации.

    Возвращает id ghost или None если зависимостей нет.
    """
    with con.cursor() as cur:
        # Владелец оригинала
        cur.execute("SELECT user_id FROM complexes WHERE id = %s", (complex_id,))
        row = cur.fetchone()
        if not row:
            return None
        owner_id = row[0]

        # Строки complex_members, ссылающихся на этот комплекс от других пользователей
        cur.execute("""
            SELECT cm.id
            FROM complex_members cm
            JOIN complexes parent ON parent.id = cm.complex_id
            WHERE cm.child_complex_id = %s
              AND (parent.user_id IS DISTINCT FROM %s)
              AND parent.is_ghost = FALSE
        """, (complex_id, owner_id))
        dep_ids = [r[0] for r in cur.fetchall()]

        if not dep_ids:
            return None

        # Данные оригинала
        cur.execute("""
            SELECT name, description, total_workers, total_electricity_kw, likes_count
            FROM complexes WHERE id = %s
        """, (complex_id,))
        orig = cur.fetchone()
        if not orig:
            return None

        # Создать ghost-запись
        cur.execute("""
            INSERT INTO complexes
                (name, description, user_id, visibility,
                 is_ghost, ghost_of_id, ghost_likes_count, ghost_reason,
                 total_workers, total_electricity_kw)
            VALUES (%s, %s, NULL, 'public', TRUE, %s, %s, %s, %s, %s)
            RETURNING id
        """, (orig[0], orig[1], complex_id, orig[4], reason, orig[2], orig[3]))
        ghost_id = cur.fetchone()[0]

        # Скопировать узлы с маппингом old_id → new_id для рёбер
        cur.execute(
            "SELECT id FROM complex_members WHERE complex_id = %s ORDER BY id",
            (complex_id,)
        )
        old_ids = [r[0] for r in cur.fetchall()]

        cur.execute("""
            INSERT INTO complex_members
                (complex_id, child_type, child_id, recipe_id, child_complex_id,
                 multiplier, pos_x, pos_y, efficiency,
                 idle_item, idle_direction, is_manual_partial, external_ports)
            SELECT %s, child_type, child_id, recipe_id, child_complex_id,
                   multiplier, pos_x, pos_y, efficiency,
                   idle_item, idle_direction, is_manual_partial, external_ports
            FROM complex_members WHERE complex_id = %s ORDER BY id
            RETURNING id
        """, (ghost_id, complex_id))
        new_ids = [r[0] for r in cur.fetchall()]
        id_map = dict(zip(old_ids, new_ids))

        # Скопировать рёбра
        cur.execute("""
            SELECT from_member_id, to_member_id, resource_item, lcm_mode
            FROM complex_edges WHERE complex_id = %s
        """, (complex_id,))
        edges = cur.fetchall()
        to_insert = [
            (ghost_id, id_map[e[0]], id_map[e[1]], e[2], e[3])
            for e in edges if e[0] in id_map and e[1] in id_map
        ]
        if to_insert:
            cur.executemany("""
                INSERT INTO complex_edges
                    (complex_id, from_member_id, to_member_id, resource_item, lcm_mode)
                VALUES (%s, %s, %s, %s, %s)
            """, to_insert)

        # Скопировать агрегированные потоки
        cur.execute("""
            INSERT INTO resource_flows
                (parent_type, parent_id, recipe_id, complex_id,
                 item_id, direction, qty_per_cycle, qty_per_min, sort_order)
            SELECT parent_type, %s, recipe_id, %s,
                   item_id, direction, qty_per_cycle, qty_per_min, sort_order
            FROM resource_flows WHERE parent_type = 1 AND complex_id = %s
        """, (ghost_id, ghost_id, complex_id))

        # Скопировать техобслуживание
        cur.execute("""
            INSERT INTO complex_maintenance (complex_id, item, rate_per_min)
            SELECT %s, item, rate_per_min FROM complex_maintenance WHERE complex_id = %s
        """, (ghost_id, complex_id))

        # Переключить зависимые узлы на ghost
        cur.execute("""
            UPDATE complex_members
            SET child_complex_id = %s, child_id = %s
            WHERE id = ANY(%s)
        """, (ghost_id, ghost_id, dep_ids))

        return ghost_id


def _auto_subscribe_community_nodes(con, complex_id: int, user_id: int) -> None:
    """Автоподписка на публичные комплексы сообщества, использованные как узлы."""
    with con.cursor() as cur:
        cur.execute("""
            INSERT INTO complex_subscriptions (user_id, complex_id)
            SELECT DISTINCT %s, cm.child_complex_id
            FROM complex_members cm
            JOIN complexes c ON c.id = cm.child_complex_id
            WHERE cm.complex_id = %s
              AND cm.child_type = 1
              AND c.visibility = 'public'
              AND c.is_ghost = FALSE
              AND c.user_id IS DISTINCT FROM %s
            ON CONFLICT DO NOTHING
        """, (user_id, complex_id, user_id))


# ─────────────────────────────────────────────────────────────────
# API: подписки на комплексы сообщества
# ─────────────────────────────────────────────────────────────────

@app.route("/api/complex/<int:complex_id>/subscribe", methods=["POST", "DELETE"])
@limiter.limit("120 per hour")
def api_complex_subscribe(complex_id: int):
    """Подписка / отписка от публичного комплекса сообщества."""
    if not g.get("user") or g.user.get("is_guest"):
        return jsonify({"error": "login_required"}), 401

    uid = g.user["id"]
    with get_db() as con:
        with con.cursor() as cur:
            if request.method == "POST":
                cur.execute(
                    "SELECT user_id FROM complexes WHERE id = %s"
                    " AND visibility = 'public' AND is_ghost = FALSE",
                    (complex_id,)
                )
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "not found or not public"}), 404
                if row[0] == uid:
                    return jsonify({"error": "cannot subscribe to own complex"}), 400
                cur.execute("""
                    INSERT INTO complex_subscriptions (user_id, complex_id)
                    VALUES (%s, %s) ON CONFLICT DO NOTHING
                """, (uid, complex_id))
            else:
                cur.execute(
                    "DELETE FROM complex_subscriptions WHERE user_id = %s AND complex_id = %s",
                    (uid, complex_id)
                )
        con.commit()
    return jsonify({"ok": True, "subscribed": request.method == "POST"})


@app.route("/api/complex/<int:complex_id>/visibility", methods=["PATCH"])
def api_complex_visibility(complex_id: int):
    """Изменить видимость комплекса (private ↔ public), опционально каскадно."""
    data = request.get_json(silent=True) or {}
    visibility = data.get("visibility")
    cascade    = bool(data.get("cascade", False))
    if visibility not in ("private", "public"):
        return jsonify({"error": "visibility must be private or public"}), 400

    # Гости не могут публиковать в Community — нужна реальная авторизация
    if visibility == "public" and g.user.get("is_guest"):
        return jsonify({"error": "login_required", "reason": "publish"}), 401

    with get_db() as con:
        with con.cursor() as cur:
            if not cascade:
                # Shadow fork для всех, кто зависит от этого комплекса (только при приватизации)
                if visibility == "private":
                    _create_shadow_fork(con, complex_id, "privatized")
                cur.execute(
                    "UPDATE complexes SET visibility = %s WHERE id = %s AND user_id = %s RETURNING id",
                    (visibility, complex_id, g.user["id"]),
                )
                if not cur.fetchone():
                    return jsonify({"error": "not found or forbidden"}), 404
            elif visibility == "public":
                # Каскадная публикация: этот комплекс + все вложенные рекурсивно
                cur.execute("""
                    WITH RECURSIVE children AS (
                        SELECT %s::int AS id
                        UNION ALL
                        SELECT cm.child_complex_id
                        FROM complex_members cm
                        JOIN children c ON cm.complex_id = c.id
                        WHERE cm.child_complex_id IS NOT NULL
                    )
                    UPDATE complexes SET visibility = 'public'
                    WHERE id IN (SELECT id FROM children WHERE id IS NOT NULL)
                      AND user_id = %s
                """, (complex_id, g.user["id"]))
                if cur.rowcount == 0:
                    return jsonify({"error": "not found or forbidden"}), 404
            else:
                # Каскадное снятие: этот комплекс + все родители рекурсивно
                # Сначала получаем список всех затронутых ID
                cur.execute("""
                    WITH RECURSIVE parents AS (
                        SELECT %s::int AS id
                        UNION ALL
                        SELECT cm.complex_id
                        FROM complex_members cm
                        JOIN parents p ON cm.child_complex_id = p.id
                    )
                    SELECT DISTINCT id FROM parents WHERE id IS NOT NULL
                """, (complex_id,))
                ids_to_privatize = [r[0] for r in cur.fetchall()]
                # Shadow fork для каждого из них
                for cid in ids_to_privatize:
                    _create_shadow_fork(con, cid, "privatized")
                # Применяем приватизацию
                cur.execute("""
                    UPDATE complexes SET visibility = 'private'
                    WHERE id = ANY(%s) AND user_id = %s
                """, (ids_to_privatize, g.user["id"]))
                if cur.rowcount == 0:
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
        if MAX_COMPLEXES and count >= MAX_COMPLEXES:
            return jsonify({
                "error":   "limit_reached",
                "limit":   MAX_COMPLEXES,
                "message": f"Maximum {MAX_COMPLEXES} complexes per account. Delete old ones to create new.",
            }), 403

    try:
        with get_db() as con:
            with con.cursor() as cur:
                # Проверить что оригинал публичный ИЛИ принадлежит текущему пользователю
                cur.execute(
                    "SELECT id, name, user_id FROM complexes WHERE id = %s AND (visibility = 'public' OR user_id = %s)",
                    (complex_id, g.user["id"]),
                )
                orig = cur.fetchone()
                if not orig:
                    return jsonify({"error": "not found or not public"}), 404

                # Локальная копия (своего же комплекса) — не привязывать к источнику
                is_self_copy = orig[2] == g.user["id"]
                fork_parent_id = None if is_self_copy else complex_id

                # Подобрать уникальное имя: "[Copy] X", "[Copy] X (2)", ...
                base_name = f"[Copy] {orig[1]}"
                cur.execute("""
                    SELECT name FROM complexes
                    WHERE user_id = %s AND (name = %s OR name LIKE %s)
                """, (g.user["id"], base_name, base_name + " (%)"))
                existing_names = {r[0] for r in cur.fetchall()}
                if base_name not in existing_names:
                    copy_name = base_name
                else:
                    n = 2
                    while f"{base_name} ({n})" in existing_names:
                        n += 1
                    copy_name = f"{base_name} ({n})"

                # Создать копию (локальные копии не хранят ссылку на оригинал)
                cur.execute("""
                    INSERT INTO complexes (name, user_id, visibility, forked_from_id)
                    VALUES (%s, %s, 'private', %s)
                    RETURNING id
                """, (copy_name, g.user["id"], fork_parent_id))
                new_id = cur.fetchone()[0]

                # Скопировать члены; сохранить маппинг old_id → new_id для рёбер
                cur.execute(
                    "SELECT id FROM complex_members WHERE complex_id = %s ORDER BY id",
                    (complex_id,),
                )
                old_member_ids = [r[0] for r in cur.fetchall()]

                cur.execute("""
                    INSERT INTO complex_members
                        (complex_id, child_type, child_id, recipe_id, child_complex_id,
                         multiplier, pos_x, pos_y, efficiency, idle_item, idle_direction,
                         is_manual_partial, external_ports)
                    SELECT %s, child_type, child_id, recipe_id, child_complex_id,
                           multiplier, pos_x, pos_y, efficiency, idle_item, idle_direction,
                           is_manual_partial, external_ports
                    FROM complex_members
                    WHERE complex_id = %s ORDER BY id
                    RETURNING id
                """, (new_id, complex_id))
                new_member_ids = [r[0] for r in cur.fetchall()]
                id_map = dict(zip(old_member_ids, new_member_ids))

                # Скопировать рёбра с пересчитанными member_id
                cur.execute("""
                    SELECT from_member_id, to_member_id, resource_item, lcm_mode
                    FROM complex_edges WHERE complex_id = %s
                """, (complex_id,))
                old_edges = cur.fetchall()
                edges_to_copy = [
                    (new_id, id_map[e[0]], id_map[e[1]], e[2], e[3])
                    for e in old_edges
                    if e[0] in id_map and e[1] in id_map
                ]
                if edges_to_copy:
                    cur.executemany("""
                        INSERT INTO complex_edges
                            (complex_id, from_member_id, to_member_id, resource_item, lcm_mode)
                        VALUES (%s, %s, %s, %s, %s)
                    """, edges_to_copy)

                cur.execute("SELECT recalculate_complex(%s)", (new_id,))
                cur.execute("SELECT slug FROM complexes WHERE id = %s", (new_id,))
                new_slug = str(cur.fetchone()[0])
            con.commit()
    except Exception as e:
        return _err500(e)

    return jsonify({"ok": True, "id": new_id, "slug": new_slug}), 201


@app.route("/api/complex/<int:complex_id>/like", methods=["POST", "DELETE"])
@limiter.limit("60 per hour")
def api_complex_like(complex_id: int):
    """Поставить / убрать лайк."""
    if not g.get("user") or g.user.get("is_guest"):
        return jsonify({"error": "login_required"}), 401
    user_id = g.user["id"]
    with get_db() as con:
        with con.cursor() as cur:
            # Проверить что комплекс существует и публичный
            cur.execute(
                "SELECT id FROM complexes WHERE id = %s AND visibility = 'public'",
                (complex_id,),
            )
            if not cur.fetchone():
                return jsonify({"error": "not found"}), 404
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
                    """SELECT c.id, c.name, c.description, c.user_id, c.visibility,
                              c.forked_from_id, c.likes_count,
                              cf.slug AS forked_from_slug,
                              c.is_ghost, c.ghost_of_id, c.ghost_reason, c.ghost_likes_count,
                              ghost_orig.slug AS ghost_of_slug,
                              (ghost_orig.visibility = 'public') AS ghost_of_visible,
                              u.display_name AS author_name
                       FROM complexes c
                       LEFT JOIN complexes cf ON cf.id = c.forked_from_id
                       LEFT JOIN complexes ghost_orig ON ghost_orig.id = c.ghost_of_id
                       LEFT JOIN users u ON u.id = c.user_id
                       WHERE c.id = %s""",
                    (complex_id,),
                )
                cx = cur.fetchone()
                if not cx:
                    return jsonify({"error": "not found"}), 404

                # Доступ: UUID-slug является достаточной защитой — проверка не нужна
                cx = dict(cx)

                cur.execute("""
                    SELECT
                        cm.id, cm.child_type, cm.child_id,
                        cm.multiplier, cm.pos_x, cm.pos_y,
                        cm.efficiency, cm.idle_item, cm.idle_direction, cm.is_manual_partial,
                        cm.external_ports,
                        r.machine_name,
                        b.workers,
                        b.electricity_kw * COALESCE(r.power_multiplier, 1.0) AS electricity_kw,
                        b.computing_tf,
                        c2.name             AS complex_name,
                        c2.slug             AS complex_slug,
                        c2.total_workers        AS complex_workers,
                        c2.total_electricity_kw AS complex_electricity_kw,
                        c2.total_computing_tf   AS complex_computing_tf,
                        c2.user_id          AS complex_owner_id,
                        c2.is_ghost         AS complex_is_ghost,
                        c2.ghost_reason     AS complex_ghost_reason,
                        c2.ghost_of_id      AS complex_ghost_of_id,
                        ghost_orig.slug     AS complex_ghost_of_slug,
                        ghost_orig.visibility::text AS complex_ghost_of_visibility,
                        inp.items  AS inputs,
                        out.items  AS outputs,
                        mnt.items  AS maintenance,
                        mnt_cx.items AS complex_maintenance,
                        constr.items AS construction,
                        constr_cx.items AS cx_construction
                    FROM complex_members cm
                    LEFT JOIN recipes   r  ON r.id  = cm.recipe_id
                    LEFT JOIN buildings b  ON b.id  = r.machine_id
                    LEFT JOIN complexes c2 ON c2.id = cm.child_complex_id
                    LEFT JOIN complexes ghost_orig ON ghost_orig.id = c2.ghost_of_id

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

                    LEFT JOIN LATERAL (
                        SELECT json_agg(json_build_object(
                            'item', cmnt.item, 'rate_per_min', cmnt.rate_per_min)
                            ORDER BY cmnt.item) AS items
                        FROM complex_maintenance cmnt WHERE cmnt.complex_id = cm.child_complex_id
                    ) mnt_cx ON (cm.child_type = 1)

                    LEFT JOIN LATERAL (
                        SELECT json_agg(json_build_object(
                            'item', bc.item, 'qty', bc.qty)
                            ORDER BY bc.item) AS items
                        FROM building_construction bc WHERE bc.building_id = b.id
                    ) constr ON (cm.child_type = 0)

                    LEFT JOIN LATERAL (
                        SELECT json_agg(json_build_object('item', cc.item, 'qty', cc.qty) ORDER BY cc.item) AS items
                        FROM complex_construction cc WHERE cc.complex_id = cm.child_complex_id
                    ) constr_cx ON (cm.child_type = 1)

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

                # Теги
                cur.execute("""
                    SELECT t.name FROM complex_tags ct
                    JOIN tags t ON t.id = ct.tag_id
                    WHERE ct.complex_id = %s ORDER BY t.name
                """, (cx["id"],))
                cx_tags = [r['name'] for r in cur.fetchall()]

                viewer_id = g.user["id"] if g.get("user") and not g.user.get("is_guest") else None
                liked = False
                subscribed = False
                if viewer_id:
                    cur.execute(
                        "SELECT 1 FROM complex_likes WHERE user_id = %s AND complex_id = %s",
                        (viewer_id, cx["id"]),
                    )
                    liked = bool(cur.fetchone())
                    cur.execute(
                        "SELECT 1 FROM complex_subscriptions WHERE user_id = %s AND complex_id = %s",
                        (viewer_id, cx["id"]),
                    )
                    subscribed = bool(cur.fetchone())

        trans = getattr(g, "content_trans", {})

        def _f(v):
            return float(v) if isinstance(v, decimal.Decimal) else v

        nodes = []
        for m in members:
            m = dict(m)
            is_complex = (m["child_type"] == 1)
            if is_complex:
                sf = sub_flows.get(m["child_id"], {"inputs": [], "outputs": []})
                inp_list     = _add_item_display(sf["inputs"])
                out_list     = _add_item_display(sf["outputs"])
                mnt_list     = _add_item_display(_parse_json_list(m["complex_maintenance"]))
                workers_val  = _f(m["complex_workers"])        if m.get("complex_workers")        is not None else None
                elec_val     = _f(m["complex_electricity_kw"]) if m.get("complex_electricity_kw")  is not None else None
                computing_val = _f(m["complex_computing_tf"])  if m.get("complex_computing_tf")    is not None else None
                constr_list  = _add_item_display(_parse_json_list(m.get("cx_construction")))
            else:
                inp_list     = _add_item_display(_parse_json_list(m["inputs"]))
                out_list     = _add_item_display(_parse_json_list(m["outputs"]))
                mnt_list     = _add_item_display(_parse_json_list(m["maintenance"]))
                workers_val  = _f(m["workers"])       if m["workers"]       is not None else None
                elec_val     = _f(m["electricity_kw"]) if m["electricity_kw"] is not None else None
                computing_val = _f(m["computing_tf"])  if m.get("computing_tf") is not None else None
                constr_list  = _add_item_display(_parse_json_list(m.get("construction")))

            raw_label = m["machine_name"] or m["complex_name"] or "?"
            label = trans.get(raw_label, raw_label) if trans and m["machine_name"] else raw_label

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
                "label":            label,
                "node_ref_slug":    str(m["complex_slug"]) if m.get("complex_slug") else None,
                "workers":          workers_val,
                "electricity_kw":   elec_val,
                "computing_tf":     computing_val,
                "inputs":           inp_list,
                "outputs":          out_list,
                "maintenance":      mnt_list,
                "construction":     constr_list,
                # Ownership + Ghost info (только для complex-узлов)
                "owner_id":         m.get("complex_owner_id") if is_complex else None,
                "is_ghost":         bool(m.get("complex_is_ghost")) if is_complex else False,
                "ghost_reason":     m.get("complex_ghost_reason") if is_complex else None,
                "ghost_of_id":      m.get("complex_ghost_of_id") if is_complex else None,
                "ghost_of_slug":    str(m["complex_ghost_of_slug"]) if (is_complex and m.get("complex_ghost_of_slug")) else None,
                "ghost_of_visible": (m.get("complex_ghost_of_visibility") == "public") if is_complex else False,
            })

        if trans:
            for e in edges:
                if e.get("resource_item") and e["resource_item"] in trans:
                    e["resource_item_display"] = trans[e["resource_item"]]

        is_ghost_cx = bool(cx.get("is_ghost"))
        return jsonify({
            "id":             cx["id"],
            "name":           cx["name"],
            "description":    cx["description"],
            "visibility":     cx["visibility"],
            "forked_from_id":   cx.get("forked_from_id"),
            "forked_from_slug": cx.get("forked_from_slug"),
            "likes_count":    cx.get("likes_count", 0),
            "_liked":         liked and not is_ghost_cx,
            "_subscribed":    subscribed,
            "is_owner":       (not is_ghost_cx) and g.get("user") and g.user["id"] == cx["user_id"],
            "author_name":    cx.get("author_name"),
            "tags":           cx_tags,
            # Ghost fields for top-level complex
            "is_ghost":           is_ghost_cx,
            "ghost_reason":       cx.get("ghost_reason"),
            "ghost_of_id":        cx.get("ghost_of_id"),
            "ghost_likes_count":  cx.get("ghost_likes_count"),
            "ghost_of_slug":      str(cx["ghost_of_slug"]) if cx.get("ghost_of_slug") else None,
            "ghost_of_visible":   bool(cx.get("ghost_of_visible")),
            "nodes":          nodes,
            "edges":          edges,
        })
    except Exception as e:
        return _err500(e)


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
            creator_ip = data.get("_creator_ip")
            fp_token   = data.get("_fp_token")
            cur.execute(
                "INSERT INTO complexes (name, user_id, creator_ip, fp_token) "
                "VALUES (%s, %s, %s::inet, %s) RETURNING id",
                (name, user_id, creator_ip, fp_token),
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

        # Теги
        if "tags" in data:
            raw = data["tags"] if isinstance(data["tags"], list) else []
            normalized = []
            for t in raw[:12]:
                t = re.sub(r'[^\w\-]', '', str(t).lower().strip())[:30]
                if len(t) >= 2 and t not in normalized:
                    normalized.append(t)
            normalized = normalized[:10]
            # upsert tag names
            for tname in normalized:
                cur.execute(
                    "INSERT INTO tags(name) VALUES(%s) ON CONFLICT(name) DO NOTHING",
                    (tname,)
                )
            # replace complex_tags
            cur.execute("DELETE FROM complex_tags WHERE complex_id = %s", (complex_id,))
            if normalized:
                cur.execute("""
                    INSERT INTO complex_tags(complex_id, tag_id)
                    SELECT %s, id FROM tags WHERE name = ANY(%s)
                """, (complex_id, normalized))

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


# ─────────────────────────────────────────────────────────────────
# API: autocomplete тегов
# ─────────────────────────────────────────────────────────────────

@app.route("/api/tags")
def api_tags():
    """Возвращает до 10 тегов, начинающихся с ?q=, отсортированных по популярности."""
    q = re.sub(r'[^\w\-]', '', request.args.get("q", "").strip().lower())
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("""
                SELECT t.name, COUNT(ct.complex_id) AS usage
                FROM tags t
                LEFT JOIN complex_tags ct ON ct.tag_id = t.id
                WHERE (%s = '' OR t.name LIKE %s)
                GROUP BY t.name
                ORDER BY usage DESC, t.name
                LIMIT 10
            """, (q, q + "%"))
            return jsonify([{"name": r[0]} for r in cur.fetchall()])


@app.route("/api/complex", methods=["POST"])
@limiter.limit("20 per hour")   # rate: не более 20 новых комплексов в час с одного IP
def api_complex_create():
    user      = g.user
    is_guest  = user.get("is_guest", False)
    client_ip = get_remote_address()
    data      = request.get_json(silent=True) or {}
    fp_token  = data.get("fp_token") or request.headers.get("X-FP-Token")

    # ── Лимиты для гостей (один блок соединения) ────────────────
    if is_guest:
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FROM complexes c
                    JOIN users u ON u.id = c.user_id WHERE u.is_guest = TRUE
                """)
                if cur.fetchone()[0] >= GUEST_GLOBAL_CAP:
                    return jsonify({
                        "error":   "service_overload",
                        "message": "Guest complex limit reached globally. Please sign in to continue.",
                    }), 503
                if client_ip:
                    cur.execute("""
                        SELECT COUNT(*) FROM complexes c
                        JOIN users u ON u.id = c.user_id
                        WHERE u.is_guest = TRUE
                          AND c.creator_ip = %s::inet
                          AND c.created_at > NOW() - INTERVAL '30 days'
                    """, (client_ip,))
                    if cur.fetchone()[0] >= GUEST_MAX_PER_IP:
                        return jsonify({
                            "error":   "guest_ip_limit",
                            "limit":   GUEST_MAX_PER_IP,
                            "message": f"Maximum {GUEST_MAX_PER_IP} guest complexes per IP per 30 days. Sign in to create more.",
                        }), 403
                if fp_token:
                    cur.execute("""
                        SELECT COUNT(*) FROM complexes c
                        JOIN users u ON u.id = c.user_id
                        WHERE u.is_guest = TRUE
                          AND c.fp_token = %s
                          AND c.created_at > NOW() - INTERVAL '30 days'
                    """, (fp_token,))
                    if cur.fetchone()[0] >= GUEST_MAX_PER_FP:
                        return jsonify({
                            "error":   "guest_fp_limit",
                            "limit":   GUEST_MAX_PER_FP,
                            "message": f"Maximum {GUEST_MAX_PER_FP} guest complexes per browser per 30 days. Sign in to create more.",
                        }), 403

    # ── Лимит залогиненных (premium без ограничений) ────────────
    elif not user.get("is_premium"):
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM complexes WHERE user_id = %s", (user["id"],))
                if MAX_COMPLEXES and cur.fetchone()[0] >= MAX_COMPLEXES:
                    return jsonify({
                        "error":   "limit_reached",
                        "limit":   MAX_COMPLEXES,
                        "message": f"Maximum {MAX_COMPLEXES} complexes per account. Delete old ones to create new.",
                    }), 403
    # Записываем IP и fp_token для anti-abuse
    data["_creator_ip"] = client_ip
    data["_fp_token"]   = fp_token
    try:
        with get_db() as con:
            cid = _save_complex_graph(con, None, data, g.user["id"])
            # Автоподписка на комплексы сообщества, использованные как узлы
            if not g.user.get("is_guest"):
                _auto_subscribe_community_nodes(con, cid, g.user["id"])
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
        return _err500(e)


@app.route("/api/complex/<int:complex_id>", methods=["PUT"])
def api_complex_update(complex_id: int):
    if not g.get("user"):
        return jsonify({"error": "login_required"}), 401
    data = request.get_json(silent=True) or {}
    try:
        with get_db() as con:
            # Shadow fork: заморозить текущее состояние для зависимых пользователей
            _create_shadow_fork(con, complex_id, "edited")
            # Сохранить новое состояние
            _save_complex_graph(con, complex_id, data, g.user["id"])
            # Автоподписка на комплексы сообщества, использованные как узлы
            if not g.user.get("is_guest"):
                _auto_subscribe_community_nodes(con, complex_id, g.user["id"])
            con.commit()
        return jsonify({"ok": True, "id": complex_id})
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "A complex with this name already exists"}), 409
    except (ValueError, PermissionError) as e:
        code = 403 if isinstance(e, PermissionError) else 400
        return jsonify({"error": str(e)}), code
    except Exception as e:
        return _err500(e)


@app.route("/api/complex/<int:complex_id>", methods=["DELETE"])
def api_complex_delete(complex_id: int):
    if not g.get("user"):
        return jsonify({"error": "login_required"}), 401
    with get_db() as con:
        with con.cursor() as cur:
            # Проверить права до shadow fork
            cur.execute(
                "SELECT id FROM complexes WHERE id = %s AND user_id = %s",
                (complex_id, g.user["id"]),
            )
            if not cur.fetchone():
                return jsonify({"error": "not found or forbidden"}), 404
        # Shadow fork: заморозить для зависимых пользователей до удаления
        _create_shadow_fork(con, complex_id, "deleted")
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM complexes WHERE id = %s AND user_id = %s",
                (complex_id, g.user["id"]),
            )
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
                        COALESCE(
                            b.electricity_kw * COALESCE(r.power_multiplier, 1.0),
                            c2.total_electricity_kw
                        )                                                          AS electricity_kw,
                        COALESCE(b.computing_tf, c2.total_computing_tf)           AS computing_tf,
                        COALESCE(b.workers, c2.total_workers)                     AS workers,
                        COALESCE(constr.items, constr_cx.items)                   AS construction
                    FROM  complex_members cm
                    LEFT  JOIN recipes   r  ON r.id  = cm.recipe_id
                    LEFT  JOIN buildings b  ON b.id  = r.machine_id
                    LEFT  JOIN complexes c2 ON c2.id = cm.child_complex_id
                    LEFT  JOIN LATERAL (
                        SELECT json_agg(
                            json_build_object('item', bc.item, 'qty', bc.qty)
                            ORDER BY bc.item
                        ) AS items
                        FROM building_construction bc WHERE bc.building_id = b.id
                    ) constr ON (cm.child_type = 0)
                    LEFT  JOIN LATERAL (
                        SELECT json_agg(json_build_object('item', cc.item, 'qty', cc.qty) ORDER BY cc.item) AS items
                        FROM complex_construction cc WHERE cc.complex_id = cm.child_complex_id
                    ) constr_cx ON (cm.child_type = 1)
                    WHERE cm.complex_id = %s
                    ORDER BY label
                """, (complex_id,))
                rows = cur.fetchall()
        trans = getattr(g, "content_trans", {})
        result = []
        for row in rows:
            row = dict(row)
            for f in ('workers', 'electricity_kw', 'efficiency', 'count', 'computing_tf'):
                if row.get(f) is not None and isinstance(row[f], decimal.Decimal):
                    row[f] = float(row[f])
            # Parse and translate construction JSON
            v = row.get("construction")
            if v is None:
                row["construction"] = []
            elif isinstance(v, str):
                row["construction"] = json.loads(v)
            row["construction"] = _add_item_display(row["construction"])
            # Перевести имя машины/комплекса на язык пользователя
            if trans and row.get("label"):
                row["label"] = trans.get(row["label"], row["label"])
            result.append(row)
        return jsonify(result)
    except Exception as e:
        return _err500(e)


# ─────────────────────────────────────────────────────────────────
# Debug: клиентский трейс (только в dev-режиме, app.debug=True)
# ─────────────────────────────────────────────────────────────────

_LOG_DIR        = os.environ.get("LOG_DIR", os.path.dirname(__file__))
_DEBUG_LOG_PATH = os.path.join(_LOG_DIR, "editor_debug.log")

@app.route("/api/debug/log", methods=["POST"])
def api_debug_log():
    if not app.debug:
        return jsonify({"error": "debug only"}), 403
    entry = request.get_json(silent=True) or {}
    line = json.dumps(entry, ensure_ascii=False)
    with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────
# CLI-команды (flask <cmd>)
# ─────────────────────────────────────────────────────────────────

import click


def _run_cleanup(sql_count: str, sql_delete: str, params: tuple,
                 dry_run: bool, dry_msg: str, empty_msg: str) -> list | None:
    """Count → dry-run check → delete → commit. Returns deleted IDs, [] if nothing, None if dry-run."""
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(sql_count, params)
            count = cur.fetchone()[0]
        if dry_run:
            click.echo(dry_msg.format(count=count))
            return None
        if count == 0:
            click.echo(empty_msg)
            return []
        with con.cursor() as cur:
            cur.execute(sql_delete, params)
            deleted_ids = [r[0] for r in cur.fetchall()]
        con.commit()
    return deleted_ids


@app.cli.command("stats")
def stats() -> None:
    """Сводка по трафику: посетители, источники, регистрации, комплексы."""
    with get_db() as con:
        with con.cursor() as cur:
            def one(sql: str) -> int:
                cur.execute(sql)
                return cur.fetchone()[0]

            visitors  = one("SELECT count(*) FROM users WHERE is_guest")
            signups   = one("SELECT count(*) FROM users WHERE NOT is_guest")
            new_24h   = one("SELECT count(*) FROM users WHERE created_at>now()-interval '24 hours'")
            new_7d    = one("SELECT count(*) FROM users WHERE created_at>now()-interval '7 days'")
            new_30d   = one("SELECT count(*) FROM users WHERE created_at>now()-interval '30 days'")
            active_7d = one("SELECT count(*) FROM users WHERE last_seen_at>now()-interval '7 days'")
            cx_total  = one("SELECT count(*) FROM complexes")
            cx_pub    = one("SELECT count(*) FROM complexes WHERE visibility='public'")
            cx_7d     = one("SELECT count(*) FROM complexes WHERE created_at>now()-interval '7 days'")

            click.echo("── Трафик ──")
            click.echo(f"Посетителей всего (гостей): {visitors}")
            click.echo(f"Зарегистрировано:           {signups}")
            click.echo(f"Новых посетителей  24ч / 7д / 30д: {new_24h} / {new_7d} / {new_30d}")
            click.echo(f"Активны за 7д:              {active_7d}")
            click.echo(f"Комплексов всего / публичных / за 7д: {cx_total} / {cx_pub} / {cx_7d}")

            click.echo("\n── Новые посетители по дням (14д) ──")
            cur.execute("""SELECT date_trunc('day', created_at)::date d, count(*)
                           FROM users WHERE created_at > now()-interval '14 days'
                           GROUP BY d ORDER BY d""")
            for d, n in cur.fetchall():
                click.echo(f"  {d}: {n}")

            click.echo("\n── По источнику перехода (utm_source) ──")
            cur.execute("""SELECT COALESCE(utm_source, '(без метки / прямой)') s, count(*)
                           FROM users GROUP BY s ORDER BY count(*) DESC LIMIT 20""")
            for s, n in cur.fetchall():
                click.echo(f"  {s}: {n}")

            click.echo("\n── Регистрации по провайдеру ──")
            cur.execute("""SELECT provider::text, count(*) FROM users WHERE NOT is_guest
                           GROUP BY provider ORDER BY count(*) DESC""")
            for p, n in cur.fetchall():
                click.echo(f"  {p}: {n}")


@app.cli.command("collect-stats")
@click.option("--day", default=None,
              help="Сутки в формате YYYY-MM-DD. По умолчанию — вчера.")
@click.option("--backfill", default=0, type=int,
              help="Восстановить N прошлых суток из created_at и выйти.")
@click.option("--dry-run", is_flag=True, help="Посчитать и показать, но не записывать.")
@click.option("--no-alerts", is_flag=True, help="Не прогонять проверки и не слать письмо.")
def collect_stats(day: str | None, backfill: int, dry_run: bool, no_alerts: bool) -> None:
    """Собирает суточный срез активности и здоровья в daily_stats.

    Запуск вручную:
        flask collect-stats
        flask collect-stats --day 2026-09-20
        flask collect-stats --backfill 60      # разово, при первой установке
        flask collect-stats --dry-run

    Cron (каждый день в 00:20 UTC, за прошедшие сутки):
        20 0 * * *  cd /app && flask collect-stats >> /var/log/coi_stats.log 2>&1
    """
    if backfill:
        written = _monitoring.backfill(backfill)
        click.echo(f"Восстановлено суток: {written}")
        return

    target = _dt.date.fromisoformat(day) if day else \
        _monitoring.today_utc() - _dt.timedelta(days=1)

    row = _monitoring.collect_day(target, dry_run=dry_run)

    click.echo(f"── Срез за {target}{' (dry-run, не записан)' if dry_run else ''} ──")
    click.echo(f"Посетителей новых / регистраций:   "
               f"{row['new_guests']} / {row['new_registered']}")
    click.echo(f"Активны 24ч / 7д:                  "
               f"{row['active_24h']} / {row['active_7d']}")
    click.echo(f"Комплексов новых / публичных / форков: "
               f"{row['cx_new']} / {row['cx_new_public']} / {row['cx_new_forks']}")
    click.echo(f"Лайков / подписок:                 {row['likes_new']} / {row['subs_new']}")
    if row.get("db_size_bytes"):
        click.echo(f"База:                              {row['db_size_bytes'] / 2**20:.0f} МБ")
    if row.get("disk_free_bytes") and row.get("disk_total_bytes"):
        pct = row["disk_free_bytes"] / row["disk_total_bytes"] * 100
        click.echo(f"Диск свободно:                     "
                   f"{row['disk_free_bytes'] / 2**30:.1f} ГБ ({pct:.0f}%)")
    click.echo(f"Сбор занял:                        {row['collect_ms']} мс")

    if dry_run or no_alerts:
        return

    fresh = _monitoring.run_checks(target, send_email=_send_admin_mail)
    if fresh:
        click.echo(f"\n── Тревоги ({len(fresh)}) ──")
        for a in fresh:
            click.echo(f"  [{a['severity']}] {a['message']}")
    else:
        click.echo("\nПроверки пройдены.")


def _send_admin_mail(subject: str, body: str) -> None:
    """Шлёт письмо всем адресам из ADMIN_EMAILS."""
    from auth import send_mail
    if not ADMIN_EMAILS:
        app.logger.warning("monitoring: ADMIN_EMAILS пуст, письмо отправить некому")
        return
    for addr in sorted(ADMIN_EMAILS):
        send_mail(addr, subject, body)


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
    where = "WHERE is_guest = TRUE AND last_seen_at < NOW() - (%s || ' months')::INTERVAL"
    deleted_ids = _run_cleanup(
        sql_count  = f"SELECT COUNT(*) FROM users {where}",
        sql_delete = f"DELETE FROM users {where} RETURNING id",
        params     = (str(months),),
        dry_run    = dry_run,
        dry_msg    = f"[dry-run] Будет удалено {{count}} гостевых аккаунтов без активности >{months} мес.",
        empty_msg  = f"Нет гостей без активности >{months} мес. Ничего не удалено.",
    )
    if deleted_ids is not None:
        click.echo(f"Удалено {len(deleted_ids)} гостевых аккаунтов (inactive >{months} мес.). "
                   f"IDs: {deleted_ids[:10]}{'...' if len(deleted_ids) > 10 else ''}")


@app.cli.command("cleanup-ghosts")
@click.option("--dry-run", is_flag=True,
              help="Показать сколько будет удалено, но не удалять.")
def cleanup_ghosts(dry_run: bool) -> None:
    """Удаляет ghost-комплексы (shadow fork), у которых не осталось зависимостей.

    Запустить вручную:
        flask cleanup-ghosts
        flask cleanup-ghosts --dry-run

    В cron (каждую ночь в 4:00):
        0 4 * * *  cd /app && flask cleanup-ghosts >> /var/log/coi_cleanup.log 2>&1
    """
    where = """
        WHERE is_ghost = TRUE
          AND NOT EXISTS (
              SELECT 1 FROM complex_members cm WHERE cm.child_complex_id = complexes.id
          )"""
    deleted_ids = _run_cleanup(
        sql_count  = f"SELECT COUNT(*) FROM complexes {where}",
        sql_delete = f"DELETE FROM complexes {where} RETURNING id",
        params     = (),
        dry_run    = dry_run,
        dry_msg    = "[dry-run] Осиротевших ghost-комплексов к удалению: {count}",
        empty_msg  = "Нет осиротевших ghost-комплексов. Ничего не удалено.",
    )
    if deleted_ids is not None:
        click.echo(f"Удалено {len(deleted_ids)} ghost-комплексов. "
                   f"IDs: {deleted_ids[:10]}{'...' if len(deleted_ids) > 10 else ''}")


@app.cli.command("send-test-email")
@click.argument("to")
def send_test_email(to: str) -> None:
    """Отправить тестовое письмо для проверки SMTP.

    Пример:
        flask send-test-email your@email.com
    """
    from auth import _send_email
    import os
    smtp_host = os.environ.get("SMTP_HOST")
    if not smtp_host:
        click.echo("SMTP_HOST not set in .env -- code will only appear in log (dev mode).")
    else:
        click.echo(f"Sending via {smtp_host} -> {to} ...")
    try:
        _send_email(to, "123456")
        if smtp_host:
            click.echo("OK: email sent. Check your inbox (and Spam folder).")
        else:
            click.echo("OK: code printed to log (SMTP not configured).")
    except Exception as e:
        click.echo(f"ERROR: {e}", err=True)
        raise SystemExit(1)


# ─────────────────────────────────────────────────────────────────
# Запуск
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import glob
    # Следим за .json файлами переводов — при изменении dev-сервер перезапустится
    extra = glob.glob(os.path.join(_i18n_dir, "*.json"))
    app.run(debug=True, port=5001, threaded=True, extra_files=extra)
