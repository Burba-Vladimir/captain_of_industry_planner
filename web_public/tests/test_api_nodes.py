"""
Тесты API /api/nodes и /api/nodes/for-resource.
"""
import pytest


class TestApiNodes:
    def test_returns_200(self, client):
        r = client.get("/api/nodes")
        assert r.status_code == 200

    def test_response_structure(self, client):
        r = client.get("/api/nodes")
        data = r.get_json()
        assert "total" in data
        assert "page" in data
        assert "pages" in data
        assert "per_page" in data
        assert "items" in data
        assert isinstance(data["items"], list)

    def test_seed_recipe_present(self, client):
        """Тестовый рецепт Test Smelter (id=9001) виден в списке."""
        r = client.get("/api/nodes?type=recipe")
        data = r.get_json()
        names = [item.get("machine_name") for item in data["items"]]
        assert "Test Smelter" in names

    def test_filter_type_recipe(self, client):
        """type=recipe возвращает только рецепты."""
        r = client.get("/api/nodes?type=recipe")
        data = r.get_json()
        for item in data["items"]:
            assert item["node_type"] == "recipe"

    def test_filter_type_complex(self, client):
        """type=complex возвращает только комплексы."""
        r = client.get("/api/nodes?type=complex")
        data = r.get_json()
        for item in data["items"]:
            assert item["node_type"] == "complex"

    def test_search_by_name(self, client):
        """Поиск по имени машины возвращает нужный рецепт."""
        r = client.get("/api/nodes?q=test+smelter")
        data = r.get_json()
        assert data["total"] >= 1
        assert any("Test Smelter" in (i.get("machine_name") or "") for i in data["items"])

    def test_search_no_result(self, client):
        """Поиск несуществующего слова возвращает пустой список."""
        r = client.get("/api/nodes?q=xyzzy_nonexistent_zzz")
        data = r.get_json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_pagination(self, client):
        """per_page=1 возвращает ровно 1 запись."""
        r = client.get("/api/nodes?per_page=1&type=recipe")
        data = r.get_json()
        assert len(data["items"]) == 1

    def test_item_fields(self, client):
        """Каждый элемент содержит обязательные поля."""
        r = client.get("/api/nodes?type=recipe")
        data = r.get_json()
        for item in data["items"]:
            assert "node_type" in item
            assert "node_id" in item
            assert "inputs" in item
            assert "outputs" in item
            assert isinstance(item["inputs"], list)
            assert isinstance(item["outputs"], list)


class TestApiNodesForResource:
    def test_returns_list(self, client):
        r = client.get("/api/nodes/for-resource?item=Test+Iron&direction=produces")
        assert r.status_code == 200
        assert isinstance(r.get_json(), list)

    def test_empty_item_returns_empty(self, client):
        r = client.get("/api/nodes/for-resource")
        assert r.status_code == 200
        assert r.get_json() == []

    def test_community_filter_returns_list(self, client):
        """type=community возвращает список (может быть пустым)."""
        r = client.get("/api/nodes/for-resource?item=Test+Iron&direction=produces&type=community")
        assert r.status_code == 200
        assert isinstance(r.get_json(), list)

    def test_recipe_produces_iron(self, client):
        """Рецепт Test Smelter производит Test Iron → должен найтись."""
        r = client.get("/api/nodes/for-resource?item=Test+Iron&direction=produces&type=recipe")
        data = r.get_json()
        labels = [i.get("machine_name") for i in data]
        assert "Test Smelter" in labels

    def test_recipe_consumes_iron_ore(self, client):
        """Рецепт Test Smelter потребляет Test Iron Ore → должен найтись."""
        r = client.get("/api/nodes/for-resource?item=Test+Iron+Ore&direction=consumes&type=recipe")
        data = r.get_json()
        labels = [i.get("machine_name") for i in data]
        assert "Test Smelter" in labels
