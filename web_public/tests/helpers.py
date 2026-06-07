"""Общие утилиты для тестов."""

SEED_RECIPE_ID = 9001


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
