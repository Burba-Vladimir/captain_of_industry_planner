"""
Тесты API скрытия/показа рецептов и комплексов.
"""
import pytest

RECIPE_ID = 9001


class TestToggleHidden:
    def test_hide_recipe(self, client):
        r = client.patch(
            f"/api/node/recipe/{RECIPE_ID}/hidden",
            json={"hidden": True},
            content_type="application/json",
        )
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_hidden_recipe_not_in_list(self, client):
        """После скрытия рецепт не появляется в списке (по умолчанию)."""
        client.patch(f"/api/node/recipe/{RECIPE_ID}/hidden", json={"hidden": True})
        r = client.get("/api/nodes?type=recipe")
        data = r.get_json()
        names = [i.get("machine_name") for i in data["items"]]
        assert "Test Smelter" not in names

    def test_show_hidden_includes_recipe(self, client):
        """С параметром hidden=true скрытый рецепт возвращается."""
        client.patch(f"/api/node/recipe/{RECIPE_ID}/hidden", json={"hidden": True})
        r = client.get("/api/nodes?type=recipe&hidden=true")
        data = r.get_json()
        names = [i.get("machine_name") for i in data["items"]]
        assert "Test Smelter" in names

    def test_unhide_recipe(self, client):
        """После снятия hidden рецепт снова виден."""
        client.patch(f"/api/node/recipe/{RECIPE_ID}/hidden", json={"hidden": True})
        client.patch(f"/api/node/recipe/{RECIPE_ID}/hidden", json={"hidden": False})
        r = client.get("/api/nodes?type=recipe")
        data = r.get_json()
        names = [i.get("machine_name") for i in data["items"]]
        assert "Test Smelter" in names

    def test_invalid_node_type_400(self, client):
        r = client.patch(
            f"/api/node/building/{RECIPE_ID}/hidden",
            json={"hidden": True},
        )
        assert r.status_code == 400

    def test_missing_hidden_field_400(self, client):
        r = client.patch(
            f"/api/node/recipe/{RECIPE_ID}/hidden",
            json={},
        )
        assert r.status_code == 400


class TestBatchHidden:
    def test_batch_hide(self, client):
        r = client.patch(
            "/api/nodes/hidden/batch",
            json={"hidden": True, "items": [{"node_type": "recipe", "node_id": RECIPE_ID}]},
        )
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_batch_show(self, client):
        client.patch(
            "/api/nodes/hidden/batch",
            json={"hidden": True, "items": [{"node_type": "recipe", "node_id": RECIPE_ID}]},
        )
        r = client.patch(
            "/api/nodes/hidden/batch",
            json={"hidden": False, "items": [{"node_type": "recipe", "node_id": RECIPE_ID}]},
        )
        assert r.status_code == 200

    def test_batch_missing_hidden_400(self, client):
        r = client.patch(
            "/api/nodes/hidden/batch",
            json={"items": [{"node_type": "recipe", "node_id": RECIPE_ID}]},
        )
        assert r.status_code == 400
