"""
Fixtures для pytest.
Требует:
  - DATABASE_URL → рабочая PostgreSQL с применённой схемой
  - SECRET_KEY   → произвольная строка
Устанавливаются через env vars или CI workflow.
"""
import os
import pytest
import psycopg2

# Установить env vars до импорта app (app.py читает os.environ["SECRET_KEY"] при импорте)
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-pytest")
os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/coi_public"
)
os.environ.setdefault("FLASK_ENV", "testing")

from app import app as flask_app  # noqa: E402 — импорт после env vars


# ─── Соединение с БД (уровень session — одно на весь прогон) ─────────────────

@pytest.fixture(scope="session")
def db_conn():
    """Прямое psycopg2-соединение для seed/cleanup."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    yield conn
    conn.close()


# ─── Минимальные игровые данные ───────────────────────────────────────────────

@pytest.fixture(scope="session")
def seed_game_data(db_conn):
    """
    Вставляет одно здание, два предмета и один рецепт.
    Удаляется после завершения тестовой сессии.
    Используем большие ID (9001+) чтобы не конфликтовать с реальными данными.
    """
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO buildings (id, name, workers, electricity_kw)
            VALUES (9001, 'Test Smelter', 5, 100)
            ON CONFLICT (id) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO items (id, name) VALUES (9001, 'Test Iron'), (9002, 'Test Iron Ore')
            ON CONFLICT (id) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO recipes (id, machine_id, machine_name, cycle_time_s, deprecated)
            VALUES (9001, 9001, 'Test Smelter', 60, FALSE)
            ON CONFLICT (id) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO resource_flows
                (parent_type, parent_id, recipe_id, item_id, direction, qty_per_cycle, qty_per_min, sort_order)
            VALUES
                (0, 9001, 9001, 9002, 0, 2, 2.0, 0),
                (0, 9001, 9001, 9001, 1, 1, 1.0, 0)
            ON CONFLICT (parent_type, parent_id, item_id, direction) DO NOTHING
        """)
    yield
    # Cleanup (в обратном порядке из-за FK)
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM resource_flows WHERE recipe_id = 9001")
        cur.execute("DELETE FROM recipes WHERE id = 9001")
        cur.execute("DELETE FROM items WHERE id IN (9001, 9002)")
        cur.execute("DELETE FROM buildings WHERE id = 9001")


# ─── Flask test client ────────────────────────────────────────────────────────

@pytest.fixture
def app():
    flask_app.config.update({"TESTING": True})
    yield flask_app


@pytest.fixture
def client(app, seed_game_data):
    """
    Test client с автоматическим guest-аккаунтом.
    Первый запрос создаёт гостя и устанавливает cookie coi_guest,
    последующие запросы используют его.
    """
    with app.test_client() as c:
        # Инициализируем guest-сессию — иначе каждый запрос создаёт нового гостя
        c.get("/")
        yield c


@pytest.fixture
def client_no_seed(app):
    """Client без seed-данных (для тестов, не требующих игровых данных)."""
    with app.test_client() as c:
        c.get("/")
        yield c
