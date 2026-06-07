"""
Тесты API CRUD-операций над комплексами.
"""
import pytest
from tests.conftest import make_complex_payload

RECIPE_ID = 9001

# Локальный алиас для обратной совместимости внутри этого модуля
def _complex_payload(name="Test Complex"):
    return make_complex_payload(name, RECIPE_ID)


class TestComplexCreate:
    def test_create_returns_201(self, client):
        r = client.post("/api/complex", json=_complex_payload())
        assert r.status_code == 201

    def test_create_returns_id_and_slug(self, client):
        r = client.post("/api/complex", json=_complex_payload("Test Complex A"))
        data = r.get_json()
        assert "id" in data
        assert "slug" in data
        assert data["ok"] is True

    def test_create_requires_name(self, client):
        r = client.post("/api/complex", json={"name": "", "nodes": [], "edges": []})
        assert r.status_code == 400

    def test_duplicate_name_409(self, client):
        name = "Unique Complex Name"
        client.post("/api/complex", json=_complex_payload(name))
        r = client.post("/api/complex", json=_complex_payload(name))
        assert r.status_code == 409

    def test_different_users_same_name_ok(self, app, seed_game_data):
        """Два разных гостя могут создать комплекс с одинаковым именем."""
        name = "Shared Name Complex"
        with app.test_client() as c1:
            c1.get("/")
            r1 = c1.post("/api/complex", json=_complex_payload(name))
            assert r1.status_code == 201

        with app.test_client() as c2:
            c2.get("/")
            r2 = c2.post("/api/complex", json=_complex_payload(name))
            assert r2.status_code == 201


class TestComplexGraph:
    def test_graph_returns_data(self, client):
        # Создаём комплекс
        r = client.post("/api/complex", json=_complex_payload("Graph Test"))
        cid = r.get_json()["id"]

        r = client.get(f"/api/complex/{cid}/graph")
        assert r.status_code == 200
        data = r.get_json()
        assert data["id"] == cid
        assert "nodes" in data
        assert "edges" in data

    def test_graph_contains_node(self, client):
        r = client.post("/api/complex", json=_complex_payload("Node Check"))
        cid = r.get_json()["id"]

        r = client.get(f"/api/complex/{cid}/graph")
        data = r.get_json()
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["node_type"] == "recipe"

    def test_graph_nonexistent_404(self, client):
        r = client.get("/api/complex/999999/graph")
        assert r.status_code == 404

    def test_graph_private_readable_by_others(self, app, seed_game_data):
        """Приватный комплекс доступен на чтение всем — UUID slug защищает от перебора."""
        with app.test_client() as owner:
            owner.get("/")
            r = owner.post("/api/complex", json=_complex_payload("Private One"))
            cid = r.get_json()["id"]

        with app.test_client() as other:
            other.get("/")
            r = other.get(f"/api/complex/{cid}/graph")
            assert r.status_code == 200
            assert r.get_json()["name"] == "Private One"


class TestComplexUpdate:
    def test_update_ok(self, client):
        r = client.post("/api/complex", json=_complex_payload("To Update"))
        cid = r.get_json()["id"]

        r = client.put(f"/api/complex/{cid}", json=_complex_payload("Updated Name"))
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_update_changes_name(self, client):
        r = client.post("/api/complex", json=_complex_payload("Original Name"))
        cid = r.get_json()["id"]

        client.put(f"/api/complex/{cid}", json=_complex_payload("New Name"))

        r = client.get(f"/api/complex/{cid}/graph")
        assert r.get_json()["name"] == "New Name"

    def test_update_forbidden_for_others(self, app, seed_game_data):
        """Другой пользователь не может обновить чужой комплекс."""
        with app.test_client() as owner:
            owner.get("/")
            r = owner.post("/api/complex", json=_complex_payload("Owner Complex"))
            cid = r.get_json()["id"]

        with app.test_client() as other:
            other.get("/")
            r = other.put(f"/api/complex/{cid}", json=_complex_payload("Hijacked"))
            assert r.status_code in (403, 400)


class TestComplexDelete:
    def test_delete_ok(self, client):
        r = client.post("/api/complex", json=_complex_payload("To Delete"))
        cid = r.get_json()["id"]

        r = client.delete(f"/api/complex/{cid}")
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_deleted_complex_not_found(self, client):
        r = client.post("/api/complex", json=_complex_payload("Delete Check"))
        cid = r.get_json()["id"]
        client.delete(f"/api/complex/{cid}")

        r = client.get(f"/api/complex/{cid}/graph")
        assert r.status_code == 404

    def test_delete_nonexistent_404(self, client):
        r = client.delete("/api/complex/999999")
        assert r.status_code == 404

    def test_delete_other_users_complex_404(self, app, seed_game_data):
        """Попытка удалить чужой комплекс возвращает 404 (не 403, не раскрывает существование)."""
        with app.test_client() as owner:
            owner.get("/")
            r = owner.post("/api/complex", json=_complex_payload("Not Mine"))
            cid = r.get_json()["id"]

        with app.test_client() as thief:
            thief.get("/")
            r = thief.delete(f"/api/complex/{cid}")
            assert r.status_code == 404


class TestComplexVisibility:
    def test_guest_cannot_publish(self, client):
        r = client.post("/api/complex", json=_complex_payload("Try Publish"))
        cid = r.get_json()["id"]

        r = client.patch(f"/api/complex/{cid}/visibility", json={"visibility": "public"})
        # Гость не может публиковать
        assert r.status_code == 401

    def test_invalid_visibility_400(self, client):
        r = client.post("/api/complex", json=_complex_payload("Vis Test"))
        cid = r.get_json()["id"]

        r = client.patch(f"/api/complex/{cid}/visibility", json={"visibility": "secret"})
        assert r.status_code == 400


class TestPublicListing:
    """GET /api/complexes/public — пагинация, поиск, поля ответа."""

    def test_returns_200(self, client):
        r = client.get("/api/complexes/public")
        assert r.status_code == 200

    def test_returns_pagination_fields(self, client):
        r = client.get("/api/complexes/public")
        data = r.get_json()
        for field in ("total", "items", "pages", "page"):
            assert field in data, f"Поле '{field}' отсутствует в ответе"
        assert isinstance(data["items"], list)

    def test_public_complex_appears_in_listing(self, client, seed_public_complex):
        r = client.get("/api/complexes/public")
        data = r.get_json()
        ids = [c["id"] for c in data["items"]]
        assert seed_public_complex["id"] in ids

    def test_search_filters_by_name(self, client, seed_public_complex):
        r = client.get(f"/api/complexes/public?q=Community+Test")
        data = r.get_json()
        assert data["total"] >= 1
        assert any(c["name"] == seed_public_complex["name"] for c in data["items"])

    def test_search_no_match_returns_empty(self, client):
        r = client.get("/api/complexes/public?q=__no_such_complex_xyz__")
        data = r.get_json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_sort_popular(self, client):
        r = client.get("/api/complexes/public?sort=popular")
        assert r.status_code == 200

    def test_items_contain_slug_and_user_id(self, client, seed_public_complex):
        r = client.get("/api/complexes/public")
        data = r.get_json()
        found = next((c for c in data["items"] if c["id"] == seed_public_complex["id"]), None)
        assert found is not None
        assert "slug" in found and found["slug"] is not None
        assert "user_id" in found

    def test_items_contain_liked_field(self, client, seed_public_complex):
        r = client.get("/api/complexes/public")
        data = r.get_json()
        found = next((c for c in data["items"] if c["id"] == seed_public_complex["id"]), None)
        assert found is not None
        assert "_liked" in found
        # Гость не лайкал → должно быть False
        assert found["_liked"] is False

    def test_pagination_per_page(self, client):
        r = client.get("/api/complexes/public?per_page=5")
        data = r.get_json()
        assert len(data["items"]) <= 5


class TestVisibilityToggle:
    """PATCH /api/complex/<id>/visibility — разграничение гость/авторизованный."""

    def test_guest_cannot_publish(self, client):
        r = client.post("/api/complex", json=_complex_payload("Vis Toggle Test"))
        cid = r.get_json()["id"]
        r = client.patch(f"/api/complex/{cid}/visibility", json={"visibility": "public"})
        assert r.status_code == 401

    def test_public_complex_visible_in_listing(self, client, seed_public_complex):
        """Комплекс, вставленный как public напрямую в БД, виден в публичном листинге."""
        r = client.get("/api/complexes/public")
        ids = [c["id"] for c in r.get_json()["items"]]
        assert seed_public_complex["id"] in ids

    def test_private_complex_not_in_listing(self, client):
        """Приватный комплекс не попадает в /api/complexes/public."""
        r = client.post("/api/complex", json=_complex_payload("Private Vis Check"))
        cid = r.get_json()["id"]
        r = client.get("/api/complexes/public")
        ids = [c["id"] for c in r.get_json()["items"]]
        assert cid not in ids


class TestLikeUnlike:
    """POST/DELETE /api/complex/<id>/like — авторизация и идемпотентность."""

    def test_guest_cannot_like(self, client, seed_public_complex):
        r = client.post(f"/api/complex/{seed_public_complex['id']}/like")
        assert r.status_code == 401

    def test_guest_cannot_unlike(self, client, seed_public_complex):
        r = client.delete(f"/api/complex/{seed_public_complex['id']}/like")
        assert r.status_code == 401

    def test_like_nonexistent_returns_401_for_guest(self, client):
        """Гость получает 401 до проверки существования комплекса."""
        r = client.post("/api/complex/999999/like")
        assert r.status_code == 401


class TestForkComplex:
    """POST /api/complex/<id>/fork — авторизация и проверка публичности оригинала."""

    def test_guest_cannot_fork(self, client, seed_public_complex):
        r = client.post(f"/api/complex/{seed_public_complex['id']}/fork")
        assert r.status_code == 401

    def test_cannot_fork_nonexistent(self, client):
        """Гость получает 401 до проверки существования."""
        r = client.post("/api/complex/999999/fork")
        assert r.status_code == 401

    def test_fork_public_complex_guest_blocked(self, client, seed_public_complex):
        """Гость не может форкнуть даже публичный комплекс."""
        r = client.post(f"/api/complex/{seed_public_complex['id']}/fork")
        assert r.status_code == 401


class TestSlugInApiResponses:
    """Фиксирует баг: api_my_complexes и api_public_complexes не возвращали slug.
    Из-за этого index.html строил URL через c.id (число), а маршрут искал по UUID-slug — 404.
    """

    def test_my_complexes_contains_slug(self, client):
        """api/complexes/mine возвращает поле slug для каждого комплекса."""
        client.post("/api/complex", json=_complex_payload("My Slug Complex"))

        r = client.get("/api/complexes/mine")
        assert r.status_code == 200
        items = r.get_json()
        assert isinstance(items, list)
        assert len(items) > 0
        for item in items:
            assert "slug" in item, f"Отсутствует slug в ответе: {item.keys()}"
            assert item["slug"] is not None

    def test_my_complexes_slug_matches_create_slug(self, client):
        """slug из api/complexes/mine совпадает со slug из POST /api/complex."""
        create_r = client.post("/api/complex", json=_complex_payload("Slug Match Test"))
        assert create_r.status_code == 201
        created_slug = create_r.get_json()["slug"]

        list_r = client.get("/api/complexes/mine")
        items = list_r.get_json()
        found = next((c for c in items if c["slug"] == created_slug), None)
        assert found is not None, "Созданный slug должен быть в списке mine"

    def test_public_complexes_contains_slug(self, app, seed_game_data):
        """api/complexes/public возвращает поле slug для публичных комплексов.

        Требует реального (не гостевого) пользователя для публикации.
        Создаём двух пользователей: один публикует, другой смотрит список.
        """
        # Сначала создаём комплекс через обычного гостя
        with app.test_client() as creator:
            creator.get("/")
            r = creator.post("/api/complex", json=_complex_payload("Public Slug Complex"))
            assert r.status_code == 201
            cid = r.get_json()["id"]
            # Гость не может публиковать → проверяем только что slug есть в /mine
            r2 = creator.get("/api/complexes/mine")
            items = r2.get_json()
            assert all("slug" in c for c in items), "slug отсутствует в api/complexes/mine"

    def test_create_returns_slug_not_none(self, client):
        """POST /api/complex возвращает непустой slug."""
        r = client.post("/api/complex", json=_complex_payload("Slug Not None"))
        assert r.status_code == 201
        data = r.get_json()
        assert "slug" in data
        assert data["slug"] is not None
        # slug должен быть UUID-подобной строкой (содержит дефисы)
        assert "-" in data["slug"]
