"""
Подключение к базе данных.
DATABASE_URL берётся из переменной окружения (или .env).
"""
from __future__ import annotations

import contextlib
import os

import psycopg2
import psycopg2.extras


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


@contextlib.contextmanager
def get_db():
    """Контекстный менеджер: открывает соединение, закрывает при выходе."""
    con = psycopg2.connect(**_db_config())
    try:
        yield con
    finally:
        con.close()


def dict_cursor(con):
    """Возвращает курсор, который отдаёт строки как dict."""
    return con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
