"""
Перенос данных гостя на реальный аккаунт — _merge_guest_to_user.

Проверяет, что при мёрдже переносятся: комплексы, скрытые рецепты и
скрытые комплексы (user_complex_prefs), а гостевой аккаунт удаляется.
"""
import uuid

import pytest

from auth import _merge_guest_to_user


@pytest.mark.usefixtures("seed_game_data")
def test_merge_migrates_complexes_and_prefs(db_conn):
    cur = db_conn.cursor()  # db_conn в autocommit-режиме (см. conftest)
    guest_id = real_id = complex_id = None
    try:
        tag = uuid.uuid4().hex[:8]

        # Гость
        gc = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO users (provider, provider_user_id, display_name, is_guest, guest_cookie) "
            "VALUES ('guest', %s, 'Guest', TRUE, %s) RETURNING id",
            (gc, gc),
        )
        guest_id = cur.fetchone()[0]

        # Реальный аккаунт
        email = f"merge-{tag}@example.com"
        cur.execute(
            "INSERT INTO users (provider, provider_user_id, display_name, email) "
            "VALUES ('email', %s, 'Real', %s) RETURNING id",
            (email, email),
        )
        real_id = cur.fetchone()[0]

        # Комплекс гостя + скрытый рецепт + скрытый комплекс
        cur.execute(
            "INSERT INTO complexes (name, user_id) VALUES (%s, %s) RETURNING id",
            (f"MergeTest {tag}", guest_id),
        )
        complex_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO user_recipe_prefs (user_id, recipe_id, hidden) VALUES (%s, 9001, TRUE)",
            (guest_id,),
        )
        cur.execute(
            "INSERT INTO user_complex_prefs (user_id, complex_id, hidden) VALUES (%s, %s, TRUE)",
            (guest_id, complex_id),
        )

        _merge_guest_to_user(guest_id, real_id)

        # Комплекс переехал на реальный аккаунт
        cur.execute("SELECT user_id FROM complexes WHERE id = %s", (complex_id,))
        assert cur.fetchone()[0] == real_id

        # Скрытый рецепт перенесён
        cur.execute(
            "SELECT hidden FROM user_recipe_prefs WHERE user_id = %s AND recipe_id = 9001",
            (real_id,),
        )
        assert cur.fetchone() == (True,)

        # Скрытый комплекс перенесён — главная проверка фикса
        cur.execute(
            "SELECT hidden FROM user_complex_prefs WHERE user_id = %s AND complex_id = %s",
            (real_id, complex_id),
        )
        assert cur.fetchone() == (True,)

        # Гость удалён
        cur.execute("SELECT 1 FROM users WHERE id = %s", (guest_id,))
        assert cur.fetchone() is None
        guest_id = None  # уже удалён мёрджем
    finally:
        if complex_id is not None:
            cur.execute("DELETE FROM user_complex_prefs WHERE complex_id = %s", (complex_id,))
            cur.execute("DELETE FROM complexes WHERE id = %s", (complex_id,))
        if real_id is not None:
            cur.execute("DELETE FROM user_recipe_prefs WHERE user_id = %s", (real_id,))
            cur.execute("DELETE FROM user_complex_prefs WHERE user_id = %s", (real_id,))
            cur.execute("DELETE FROM users WHERE id = %s", (real_id,))
        if guest_id is not None:
            cur.execute("DELETE FROM users WHERE id = %s", (guest_id,))
