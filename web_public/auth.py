"""
Аутентификация: Google OAuth 2.0, Steam OpenID, код сессии.

Использование:
    from auth import auth_bp, current_user, login_required
    app.register_blueprint(auth_bp)
"""
from __future__ import annotations

import logging
import os
import random
import smtplib
import string
import uuid
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps

import requests
from authlib.integrations.flask_client import OAuth
from flask import (Blueprint, abort, g, jsonify, redirect,
                   request, session, url_for)

from db import get_db

log = logging.getLogger(__name__)

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
# Email-авторизация (одноразовый 6-значный код)
# ─────────────────────────────────────────────────────────────────

def _gen_email_code() -> str:
    """Генерирует 6-значный цифровой код."""
    return f"{random.randint(0, 999999):06d}"


def _send_email(to: str, code: str) -> None:
    """
    Отправляет письмо с кодом.
    Если SMTP не настроен — выводит код в лог (для разработки).
    """
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    smtp_from = os.environ.get("SMTP_FROM", smtp_user or "noreply@coi-planner.app")

    subject = "Your CoI Planner login code"
    body_text = f"Your login code: {code}\n\nThis code expires in 15 minutes."
    body_html = f"""
<div style="font-family:sans-serif;max-width:420px">
  <h2 style="color:#1e40af">Captain of Industry Planner</h2>
  <p>Your login code:</p>
  <div style="font-size:2rem;font-weight:bold;letter-spacing:.3em;color:#1d4ed8;
              background:#eff6ff;border-radius:8px;padding:16px 24px;display:inline-block">
    {code}
  </div>
  <p style="color:#64748b;font-size:.85rem">Expires in 15 minutes. Do not share this code.</p>
</div>"""

    if not smtp_host:
        # Dev-режим: просто логируем
        log.warning("SMTP not configured — email code for %s: %s", to, code)
        print(f"\n{'='*40}\nEmail code for {to}: {code}\n{'='*40}\n")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_from
    msg["To"]      = to
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    with smtplib.SMTP(smtp_host, smtp_port) as srv:
        srv.ehlo()
        if smtp_port != 465:
            srv.starttls()
        if smtp_user and smtp_pass:
            srv.login(smtp_user, smtp_pass)
        srv.sendmail(smtp_from, [to], msg.as_string())


def _merge_guest_to_user(guest_id: int, real_id: int) -> None:
    """
    Переносит данные гостя (комплексы, скрытые рецепты) на реальный аккаунт.
    Гостевой пользователь удаляется после переноса.
    """
    if guest_id == real_id:
        return
    with get_db() as con:
        with con.cursor() as cur:
            # Перенести комплексы (где нет конфликта имён с существующими у real_id)
            cur.execute("""
                UPDATE complexes SET user_id = %s
                WHERE  user_id = %s
                  AND  name NOT IN (
                      SELECT name FROM complexes WHERE user_id = %s
                  )
            """, (real_id, guest_id, real_id))
            # Скопировать скрытые рецепты (игнорировать дубликаты)
            cur.execute("""
                INSERT INTO user_recipe_prefs (user_id, recipe_id, hidden)
                SELECT %s, recipe_id, hidden
                FROM   user_recipe_prefs
                WHERE  user_id = %s AND hidden = TRUE
                ON CONFLICT (user_id, recipe_id) DO NOTHING
            """, (real_id, guest_id))
            # Удалить гостя (каскад удалит оставшиеся данные)
            cur.execute("DELETE FROM users WHERE id = %s AND is_guest = TRUE", (guest_id,))
        con.commit()
    log.info("Merged guest %d into user %d", guest_id, real_id)


@auth_bp.route("/email/send", methods=["POST"])
def email_send():
    """
    Шаг 1: получить email, сгенерировать код, отправить письмо.
    Rate-limit: не более 3 кодов на один email за последние 10 минут.
    """
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()

    # Базовая валидация
    if not email or "@" not in email or len(email) > 254:
        return jsonify({"error": "invalid_email"}), 400

    # Rate-limit
    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM email_codes
                WHERE  email = %s AND created_at > NOW() - INTERVAL '10 minutes'
            """, (email,))
            if cur.fetchone()[0] >= 3:
                return jsonify({"error": "too_many_requests"}), 429

    code = _gen_email_code()

    with get_db() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO email_codes (email, code) VALUES (%s, %s)",
                (email, code),
            )
        con.commit()

    try:
        _send_email(email, code)
    except Exception as e:
        log.exception("Failed to send email to %s", email)
        return jsonify({"error": "send_failed", "detail": str(e)}), 500

    return jsonify({"ok": True})


@auth_bp.route("/email/verify", methods=["POST"])
def email_verify():
    """
    Шаг 2: проверить код, войти / создать аккаунт, смержить гостя.
    """
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    code  = (data.get("code")  or "").strip()

    if not email or not code or len(code) != 6:
        return jsonify({"error": "invalid_input"}), 400

    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("""
                UPDATE email_codes
                SET    used_at = NOW()
                WHERE  email = %s AND code = %s
                  AND  used_at IS NULL
                  AND  expires_at > NOW()
                RETURNING id
            """, (email, code))
            row = cur.fetchone()
        con.commit()

    if not row:
        return jsonify({"error": "invalid_or_expired_code"}), 400

    # Имя по умолчанию — часть email до @
    display_name = email.split("@")[0]
    user_id = _upsert_user(
        provider="email",
        provider_user_id=email,
        display_name=display_name,
        avatar_url=None,
        email=email,
    )

    # Merge гостя если был
    guest_user = g.get("user")
    if guest_user and guest_user.get("is_guest"):
        _merge_guest_to_user(guest_user["id"], user_id)
        # Сбросить гостевой cookie
        session.pop("_seen_updated", None)

    session["user_id"] = user_id
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
