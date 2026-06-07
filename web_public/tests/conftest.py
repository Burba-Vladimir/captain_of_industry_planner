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

# ── Отключаем rate limiter для тестов ────────────────────────────────────────
# Flask-Limiter читает RATELIMIT_ENABLED из app.config перед каждым запросом,
# поэтому достаточно выставить флаг на уровне модуля — до первого запроса.
flask_app.config["RATELIMIT_ENABLED"] = False


# ─── Соединение с БД (уровень session — одно на весь прогон) ─────────────────

@pytest.fixture(scope="session")
def db_conn():
    """Прямое psycopg2-соединение для seed/cleanup."""
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    yield conn
    conn.close()


# ─── Сброс счётчиков rate limiter перед каждым тестом ────────────────────────

@pytest.fixture(autouse=True)
def reset_rate_limits():
    """Сбрасывает in-memory счётчики Flask-Limiter перед каждым тестом."""
    try:
        from app import limiter
        limiter._storage.reset()
    except Exception:
        pass
    yield


# ─── Очистка гостевых пользователей и всех их данных ─────────────────────────

@pytest.fixture(autouse=True)
def cleanup_guests(db_conn):
    """Удаляет гостевых пользователей, созданных ВНУТРИ теста.
    Запоминает максимальный user_id до теста и чистит только тех, кто появился позже.
    Так не затрагиваются реальные гостевые сессии разработчика."""
    with db_conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) FROM users WHERE is_guest = TRUE")
        max_id_before = cur.fetchone()[0]

    yield

    with db_conn.cursor() as cur:
        # Удаляем все связанные данные в правильном порядке (FK constraints)
        cur.execute("""
            DELETE FROM complex_likes
            WHERE complex_id IN (
                SELECT id FROM complexes
                WHERE user_id IN (SELECT id FROM users WHERE is_guest = TRUE AND id > %s)
            )
        """, (max_id_before,))
        cur.execute("""
            DELETE FROM complex_members
            WHERE complex_id IN (
                SELECT id FROM complexes
                WHERE user_id IN (SELECT id FROM users WHERE is_guest = TRUE AND id > %s)
            )
        """, (max_id_before,))
        cur.execute("""
            DELETE FROM resource_flows
            WHERE complex_id IN (
                SELECT id FROM complexes
                WHERE user_id IN (SELECT id FROM users WHERE is_guest = TRUE AND id > %s)
            )
        """, (max_id_before,))
        cur.execute("""
            DELETE FROM complexes
            WHERE user_id IN (SELECT id FROM users WHERE is_guest = TRUE AND id > %s)
        """, (max_id_before,))
        cur.execute("""
            DELETE FROM user_recipe_prefs
            WHERE user_id IN (SELECT id FROM users WHERE is_guest = TRUE AND id > %s)
        """, (max_id_before,))
        cur.execute(
            "DELETE FROM users WHERE is_guest = TRUE AND id > %s",
            (max_id_before,),
        )


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
        # Удаляем ВСЕ resource_flows с нашими тестовыми items —
        # в т.ч. parent_type=1 (комплексные потоки от recalculate_complex)
        cur.execute("DELETE FROM resource_flows WHERE item_id IN (9001, 9002)")
        cur.execute("DELETE FROM recipes WHERE id = 9001")
        cur.execute("DELETE FROM items WHERE id IN (9001, 9002)")
        cur.execute("DELETE FROM buildings WHERE id = 9001")


SEED_RECIPE_ID = 9001  # id из seed_game_data


def make_complex_payload(name="Test Complex", recipe_id=SEED_RECIPE_ID):
    """Минимальный payload для создания комплекса через API."""
    return {
        "name": name,
        "nodes": [
            {
                "_id": "node-1",
                "node_type": "recipe",
                "node_ref_id": recipe_id,
                "count": 1,
                "pos_x": 100,
                "pos_y": 100,
                "efficiency": 1.0,
            }
        ],
        "edges": [],
    }


# ─── Публичный комплекс для тестов Community ─────────────────────────────────

@pytest.fixture
def seed_public_complex(db_conn):
    """Вставляет публичный комплекс напрямую в БД (обходя гостевое ограничение на публикацию).
    Используется в тестах Community-листинга, лайков и форка."""
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO users (provider, provider_user_id, display_name, is_guest)
            VALUES ('email', 'test-community-seed@example.com', 'Community Tester', FALSE)
            RETURNING id
        """)
        uid = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO complexes (name, user_id, visibility)
            VALUES ('Community Test Complex', %s, 'public')
            RETURNING id
        """, (uid,))
        cid = cur.fetchone()[0]
    yield {"id": cid, "name": "Community Test Complex", "user_id": uid}
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM complex_likes WHERE complex_id = %s", (cid,))
        cur.execute("DELETE FROM resource_flows WHERE complex_id = %s", (cid,))
        cur.execute("DELETE FROM complex_members WHERE complex_id = %s", (cid,))
        cur.execute("DELETE FROM complexes WHERE id = %s", (cid,))
        cur.execute("DELETE FROM users WHERE id = %s", (uid,))


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
