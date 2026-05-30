"""
Аутентификация: Google OAuth 2.0, Steam OpenID, код сессии.

Использование:
    from auth import auth_bp, current_user, login_required
    app.register_blueprint(auth_bp)
"""
from __future__ import annotations

import os
import random
import string
import uuid
from functools import wraps

import requests
from authlib.integrations.flask_client import OAuth
from flask import (Blueprint, abort, g, jsonify, redirect,
                   request, session, url_for)

from db import get_db

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")
oauth    = OAuth()

# ─────────────────────────────────────────────────────────────────
# Инициализация OAuth-провайдеров
# ─────────────────────────────────────────────────────────────────

def init_oauth(app):
    oauth.init_app(app)

    # Google OAuth — регистрируем только если credentials заданы
    _google_id     = os.environ.get("GOOGLE_CLIENT_ID")
    _google_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if _google_id and _google_secret:
        oauth.register(
            name="google",
            client_id=_google_id,
            client_secret=_google_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    # Steam использует OpenID 2.0 (не OAuth 2.0), обрабатываем вручную
    # (authlib не поддерживает OpenID 2.0 напрямую)


# ─────────────────────────────────────────────────────────────────
# Хелперы: текущий пользователь
# ─────────────────────────────────────────────────────────────────

def _row_to_user(row) -> dict:
    """Конвертирует строку из БД в словарь пользователя."""
    return {
        "id":           row[0],
        "display_name": row[1],
        "avatar_url":   row[2],
        "email":        row[3],
        "is_premium":   row[4],
        "is_guest":     row[5],
    }


def _load_user_from_session():
    """Загружает пользователя из сессии Flask в g.user."""
    user_id = session.get("user_id")
    if not user_id:
        g.user = None
        return
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT id, display_name, avatar_url, email, is_premium, is_guest "
                "FROM users WHERE id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    if row:
        g.user = _row_to_user(row)
        # Обновить last_seen_at раз в сессию
        if not session.get("_seen_updated"):
            with get_db() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET last_seen_at = NOW() WHERE id = %s", (user_id,)
                    )
                con.commit()
            session["_seen_updated"] = True
    else:
        g.user = None
        session.clear()


def load_guest_by_cookie(cookie_val: str) -> dict | None:
    """Загружает гостевого пользователя по UUID-cookie. None если не найден."""
    try:
        uuid.UUID(cookie_val)
    except (ValueError, AttributeError):
        return None
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT id, display_name, avatar_url, email, is_premium, is_guest "
                "FROM users WHERE guest_cookie = %s AND is_guest = TRUE",
                (cookie_val,),
            )
            row = cur.fetchone()
    return _row_to_user(row) if row else None


def create_guest_user() -> tuple[dict, str]:
    """Создаёт нового гостевого пользователя. Возвращает (user_dict, cookie_value)."""
    cookie_val = str(uuid.uuid4())
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO users
                    (provider, provider_user_id, display_name, is_guest, guest_cookie)
                VALUES ('guest', %s, 'Guest', TRUE, %s)
                RETURNING id
            """, (cookie_val, cookie_val))
            user_id = cur.fetchone()[0]
        con.commit()
    return {
        "id":           user_id,
        "display_name": "Guest",
        "avatar_url":   None,
        "email":        None,
        "is_premium":   False,
        "is_guest":     True,
    }, cookie_val


@property
def current_user():
    return g.get("user")


def login_required(f):
    """Декоратор: отдаёт 401 если пользователь не авторизован."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not g.get("user"):
            abort(401)
        return f(*args, **kwargs)
    return wrapper


def _upsert_user(provider: str, provider_user_id: str,
                 display_name: str, avatar_url: str | None,
                 email: str | None) -> int:
    """Создаёт или обновляет пользователя, возвращает его id."""
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO users (provider, provider_user_id, display_name, avatar_url, email)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (provider, provider_user_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    avatar_url   = EXCLUDED.avatar_url,
                    email        = COALESCE(EXCLUDED.email, users.email),
                    last_seen_at = NOW()
                RETURNING id
            """, (provider, provider_user_id, display_name, avatar_url, email))
            user_id = cur.fetchone()[0]
        con.commit()
    return user_id


# ─────────────────────────────────────────────────────────────────
# Google OAuth
# ─────────────────────────────────────────────────────────────────

@auth_bp.route("/google/login")
def google_login():
    redirect_uri = url_for("auth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/google/callback")
def google_callback():
    token    = oauth.google.authorize_access_token()
    userinfo = token.get("userinfo") or oauth.google.userinfo()
    user_id  = _upsert_user(
        provider="google",
        provider_user_id=userinfo["sub"],
        display_name=userinfo.get("name", "Unknown"),
        avatar_url=userinfo.get("picture"),
        email=userinfo.get("email"),
    )
    session["user_id"] = user_id
    return redirect(url_for("index"))


# ─────────────────────────────────────────────────────────────────
# Steam OpenID 2.0
# ─────────────────────────────────────────────────────────────────

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
STEAM_API_SUMMARY = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"


@auth_bp.route("/steam/login")
def steam_login():
    callback = url_for("auth.steam_callback", _external=True)
    params = {
        "openid.ns":         "http://specs.openid.net/auth/2.0",
        "openid.mode":       "checkid_setup",
        "openid.return_to":  callback,
        "openid.realm":      request.host_url,
        "openid.identity":   "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return redirect(f"{STEAM_OPENID_URL}?{qs}")


@auth_bp.route("/steam/callback")
def steam_callback():
    # Проверка подписи Steam
    params = dict(request.args)
    params["openid.mode"] = "check_authentication"
    resp = requests.post(STEAM_OPENID_URL, data=params, timeout=10)
    if "is_valid:true" not in resp.text:
        abort(400, "Steam authentication failed")

    # Извлекаем Steam ID из claimed_id
    claimed = request.args.get("openid.claimed_id", "")
    steam_id = claimed.rsplit("/", 1)[-1]

    # Получаем профиль
    api_key = os.environ.get("STEAM_API_KEY", "")
    profile = {}
    if api_key:
        r = requests.get(STEAM_API_SUMMARY,
                         params={"key": api_key, "steamids": steam_id},
                         timeout=10)
        players = r.json().get("response", {}).get("players", [])
        profile = players[0] if players else {}

    user_id = _upsert_user(
        provider="steam",
        provider_user_id=steam_id,
        display_name=profile.get("personaname", f"Steam:{steam_id[:8]}"),
        avatar_url=profile.get("avatarmedium"),
        email=None,  # Steam не предоставляет email
    )
    session["user_id"] = user_id
    return redirect(url_for("index"))


# ─────────────────────────────────────────────────────────────────
# Код сессии (fallback без OAuth)
# ─────────────────────────────────────────────────────────────────

def _gen_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


@auth_bp.route("/code/new", methods=["POST"])
def new_session_code():
    """Создаёт анонимного пользователя + код сессии. Возвращает JSON с кодом."""
    code = _gen_code()
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO users (provider, provider_user_id, display_name)
                VALUES ('session_code', %s, 'Anonymous')
                RETURNING id
            """, (code,))
            user_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO session_codes (user_id, code) VALUES (%s, %s)",
                (user_id, code),
            )
        con.commit()
    session["user_id"] = user_id
    return jsonify({"code": code})


@auth_bp.route("/code/login", methods=["POST"])
def login_with_code():
    """Вход по коду сессии."""
    code = (request.json or {}).get("code", "").strip().upper()
    if len(code) != 8:
        return jsonify({"error": "Invalid code"}), 400
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("""
                UPDATE session_codes SET last_used_at = NOW()
                WHERE code = %s
                RETURNING user_id
            """, (code,))
            row = cur.fetchone()
        con.commit()
    if not row:
        return jsonify({"error": "Code not found"}), 404
    session["user_id"] = row[0]
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────────────
# Выход
# ─────────────────────────────────────────────────────────────────

@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("index"))


# ─────────────────────────────────────────────────────────────────
# API: текущий пользователь
# ─────────────────────────────────────────────────────────────────

@auth_bp.route("/me")
def me():
    if not g.get("user"):
        return jsonify(None)
    return jsonify(g.user)
