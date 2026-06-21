"""
Smoke-тесты: страницы отдают 200 и нужный контент.
"""
import pytest
from tests.conftest import make_complex_payload


class TestIndex:
    def test_index_ok(self, client):
        r = client.get("/")
        assert r.status_code == 200

    def test_index_sets_guest_cookie(self, app):
        """Новый посетитель без cookie получает coi_guest."""
        with app.test_client() as c:
            r = c.get("/")
        assert r.status_code == 200
        assert "coi_guest" in r.headers.get("Set-Cookie", "")

    def test_index_ru(self, client):
        """Переключение языка через query param."""
        r = client.get("/?lang=ru")
        assert r.status_code == 200

    def test_index_unknown_lang_ignored(self, client):
        """Неизвестный язык не роняет приложение."""
        r = client.get("/?lang=zz")
        assert r.status_code == 200


class TestPrivacy:
    def test_privacy_en(self, client):
        r = client.get("/privacy")
        assert r.status_code == 200
        assert b"Privacy Policy" in r.data
        assert b"cookie" in r.data.lower()

    def test_privacy_ru(self, client):
        r = client.get("/privacy?lang=ru")
        assert r.status_code == 200
        # Russian content rendered
        assert "Политика конфиденциальности".encode() in r.data
        assert "cookie".encode() in r.data.lower()
        # Banner: Russian text injected via t()
        assert "Политика конфиденциальности".encode() in r.data

    def test_cookie_banner_ru(self, client):
        """Cookie banner text switches to Russian on index page."""
        r = client.get("/?lang=ru")
        assert r.status_code == 200
        assert "Понятно".encode() in r.data
        assert "Политика конфиденциальности".encode() in r.data


class TestAbout:
    def test_about_en(self, client):
        r = client.get("/about")
        assert r.status_code == 200
        assert b"CoI Planner" in r.data
        assert b"Boosty" in r.data
        assert b"lava.top" in r.data

    def test_about_ru(self, client):
        r = client.get("/about?lang=ru")
        assert r.status_code == 200
        assert "О проекте".encode() in r.data
        assert b"Boosty" in r.data

    def test_about_linked_from_index(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"/about" in r.data


class TestComplexRoutes:
    def test_nonexistent_complex_edit_404(self, client):
        r = client.get("/complex/00000000-0000-0000-0000-000000000000/edit")
        assert r.status_code == 404

    def test_nonexistent_complex_view_404(self, client):
        r = client.get("/complex/00000000-0000-0000-0000-000000000000/view")
        assert r.status_code == 404

    def test_edit_route_uses_slug(self, client):
        """Маршрут /complex/{slug}/edit работает с UUID-slug из API.

        Фиксирует баг: раньше index.html строил URL с числовым c.id,
        а маршрут искал по slug — это давало 404.
        """
        r = client.post("/api/complex", json=make_complex_payload("Slug Edit Test"))
        assert r.status_code == 201
        slug = r.get_json().get("slug")
        assert slug is not None, "API должен возвращать поле slug"

        r2 = client.get(f"/complex/{slug}/edit")
        assert r2.status_code == 200

    def test_numeric_id_in_edit_route_gives_404(self, client):
        """Числовой id (не UUID slug) в URL редактора даёт 404.

        Проверяет, что маршрут ищет именно по slug, а не по id.
        """
        r = client.post("/api/complex", json=make_complex_payload("Numeric ID Test"))
        assert r.status_code == 201
        cid = r.get_json()["id"]

        r2 = client.get(f"/complex/{cid}/edit")
        assert r2.status_code == 404

    def test_view_route_uses_slug(self, client):
        """Маршрут /complex/{slug}/view работает с UUID-slug.

        Фиксирует аналогичный баг для Community-вкладки.
        Владелец перенаправляется с /view на /edit (новое поведение — auto-redirect).
        """
        r = client.post("/api/complex", json=make_complex_payload("Slug View Test"))
        assert r.status_code == 201
        slug = r.get_json().get("slug")
        assert slug is not None

        # Владелец → 302 на /edit, затем 200
        r2 = client.get(f"/complex/{slug}/view", follow_redirects=True)
        assert r2.status_code == 200
