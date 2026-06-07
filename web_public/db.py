"""
Подключение к базе данных.
DATABASE_URL берётся из переменной окружения (или .env).

Использует ThreadedConnectionPool: соединения создаются один раз и переиспользуются.
Максимум 10 одновременных соединений; каждое открывается с connect_timeout=5с.
"""
from __future__ import annotations

import contextlib
import os
import threading

import psycopg2
import psycopg2.extras
import psycopg2.pool


def _db_config() -> dict:
    url = os.environ.get("DATABASE_URL")
    if url:
        return {"dsn": url}
    # Fallback для локальной разработки
    return {
        "host":     os.environ.get("DB_HOST",     "127.0.0.1"),
        "port":     int(os.environ.get("DB_PORT", "5432")),
        "dbname":   os.environ.get("DB_NAME",     "coi_public"),
        "user":     os.environ.get("DB_USER",     "postgres"),
        "password": os.environ.get("DB_PASSWORD", "postgres"),
    }


# ─── Пул соединений ──────────────────────────────────────────────────────────
# Инициализируется при первом обращении (lazy), потокобезопасен.
# minconn=2  — всегда держим 2 соединения открытыми.
# maxconn=10 — не создаём больше 10 одновременно (защита от connection storm).

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:   # повторная проверка после захвата блокировки
            return _pool
        config = _db_config()
        # connect_timeout=5 — подключение не будет висеть дольше 5 секунд
        if "dsn" in config:
            # DSN-формат: добавляем параметр в строку
            sep = "&" if "?" in config["dsn"] else "?"
            config["dsn"] = config["dsn"] + sep + "connect_timeout=5"
        else:
            config["connect_timeout"] = 5
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            **config,
        )
    return _pool


@contextlib.contextmanager
def get_db():
    """Контекстный менеджер: берёт соединение из пула, возвращает при выходе.
    При ошибке делает rollback перед возвратом в пул.
    """
    pool = _get_pool()
    con = pool.getconn()
    try:
        yield con
    except Exception:
        con.rollback()
        raise
    finally:
        pool.putconn(con)


def dict_cursor(con):
    """Возвращает курсор, который отдаёт строки как dict."""
    return con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
