"""
Тесты API CRUD-операций над комплексами.
"""
import pytest

RECIPE_ID = 9001

# Минимальный payload для создания комплекса
def _complex_payload(name="Test Complex"):
    return {
        "name": name,
        "nodes": [
            {
                "_id": "node-1",
                "node_type": "recipe",
                "node_ref_id": RECIPE_ID,
                "count": 1,
                "pos_x": 100,
                "pos_y": 100,
                "efficiency": 1.0,
            }
        ],
        "edges": [],
    }


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

    def test_graph_private_forbidden_for_others(self, app, seed_game_data):
        """Чужой приватный комплекс недоступен."""
        with app.test_client() as owner:
            owner.get("/")
            r = owner.post("/api/complex", json=_complex_payload("Private One"))
            cid = r.get_json()["id"]

        with app.test_client() as other:
            other.get("/")
            r = other.get(f"/api/complex/{cid}/graph")
            assert r.status_code == 403


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
