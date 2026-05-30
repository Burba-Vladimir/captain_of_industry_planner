"""
Smoke-тесты: страницы отдают 200 и нужный контент.
"""
import pytest


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


class TestComplexRoutes:
    def test_nonexistent_complex_edit_404(self, client):
        r = client.get("/complex/00000000-0000-0000-0000-000000000000/edit")
        assert r.status_code == 404

    def test_nonexistent_complex_view_404(self, client):
        r = client.get("/complex/00000000-0000-0000-0000-000000000000/view")
        assert r.status_code == 404
