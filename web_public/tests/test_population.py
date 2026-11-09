"""Тесты страницы и API расчёта нужд поселения."""
import pytest


@pytest.fixture
def seed_population_need(db_conn, seed_game_data):
    """Потребность в 'Test Iron' плюс второй рецепт того же предмета без входов.

    Второй рецепт нужен, чтобы проверить правило выбора по умолчанию: первым
    должен идти вариант с наименьшим числом входов, а не самый быстрый.
    """
    with db_conn.cursor() as cur:
        # Рецепт 9002: тот же выход, БОЛЬШЕ скорость, но без входов.
        cur.execute("""
            INSERT INTO recipes (id, machine_id, machine_name, cycle_time_s, deprecated)
            VALUES (9002, 9001, 'Test Smelter', 60, FALSE)
            ON CONFLICT (id) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO resource_flows
                (parent_type, parent_id, recipe_id, item_id, direction, qty_per_cycle, qty_per_min, sort_order)
            VALUES (0, 9002, 9002, 9001, 1, 5, 5.0, 0)
            ON CONFLICT (parent_type, parent_id, item_id, direction) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO population_needs
                (item, label, need_type, qty_per_10k, unit, group_key, choice_group, is_default, sort_order)
            VALUES ('Test Iron', 'ТестЖелезо', 0, 20, 'per_min', 'goods', NULL, TRUE, 9000)
            ON CONFLICT (label) DO NOTHING
        """)
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM population_needs WHERE label = 'ТестЖелезо'")
        cur.execute("DELETE FROM resource_flows WHERE recipe_id = 9002")
        cur.execute("DELETE FROM recipes WHERE id = 9002")


def _find(payload, label):
    return next((n for n in payload["needs"] if n["label"] == label), None)


def test_population_page_renders(client):
    r = client.get("/population")
    assert r.status_code == 200
    assert b"populationCalc" in r.data


def test_population_api_shape(client):
    r = client.get("/api/population/data")
    assert r.status_code == 200
    data = r.get_json()
    assert data["base_population"] == 10000
    assert isinstance(data["needs"], list)


def test_need_exposes_recipe_with_costs(client, seed_population_need):
    need = _find(client.get("/api/population/data").get_json(), "ТестЖелезо")
    assert need is not None
    assert need["qty_per_10k"] == 20
    assert need["need_type"] == 0
    assert len(need["options"]) == 2

    with_inputs = next(o for o in need["options"] if o["recipe_id"] == 9001)
    assert with_inputs["rate"] == 1.0
    assert with_inputs["workers"] == 5
    assert with_inputs["electricity_kw"] == 100
    assert [i["item"] for i in with_inputs["inputs"]] == ["Test Iron Ore"]
    assert with_inputs["inputs"][0]["qty_per_min"] == 2.0


def test_default_option_has_fewest_inputs(client, seed_population_need):
    """Первым идёт вариант без входов, хотя по скорости он не лидер по старому правилу.

    Правило важно: самый быстрый рецепт часто оказывается вырожденным (воду
    быстрее всего «производит» очистка стоков, которых столько не бывает).
    """
    need = _find(client.get("/api/population/data").get_json(), "ТестЖелезо")
    assert need["options"][0]["recipe_id"] == 9002
    assert need["options"][0]["inputs"] == []


def test_waste_need_lists_consumers(client, db_conn, seed_game_data):
    """Для отходов кандидатами должны быть рецепты, которые предмет ПОТРЕБЛЯЮТ."""
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO population_needs
                (item, label, need_type, qty_per_10k, unit, group_key, choice_group, is_default, sort_order)
            VALUES ('Test Iron Ore', 'ТестОтход', 1, 10, 'per_min', 'utility', NULL, TRUE, 9001)
            ON CONFLICT (label) DO NOTHING
        """)
    try:
        need = _find(client.get("/api/population/data").get_json(), "ТестОтход")
        assert need["need_type"] == 1
        # Рецепт 9001 потребляет Test Iron Ore 2/мин — он и должен быть кандидатом.
        assert [o["recipe_id"] for o in need["options"]] == [9001]
        assert need["options"][0]["rate"] == 2.0
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM population_needs WHERE label = 'ТестОтход'")


def test_electricity_need_uses_the_item(client):
    """Электроэнергия — обычный предмет: генераторы производят `Electricity`.

    Раньше для неё был отдельный путь через отрицательный electricity_kw
    здания. Он убран: привязка к предмету даёт и иконку, и уровни зданий,
    и отсев по тому же правилу, что у всего остального.
    """
    data = client.get("/api/population/data").get_json()
    need = next((n for n in data["needs"] if n["unit"] == "kw"), None)
    if need is None:
        import pytest as _p
        _p.skip("в базе нет строки потребности в электроэнергии")
    assert need["item"] == "Electricity"
    assert need["options"], "должны найтись генераторы"
    assert all(o["rate"] > 0 for o in need["options"])


def test_waste_option_that_only_repacks_is_dropped(client, db_conn, seed_game_data):
    """Пресс превращает Waste в Waste Pressed — это не переработка, а перекладывание.

    Такие варианты не должны попадать в кандидаты для отходов.
    """
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO items (id, name) VALUES (9003, 'Test Iron Ore Pressed') ON CONFLICT (id) DO NOTHING")
        cur.execute("""
            INSERT INTO recipes (id, machine_id, machine_name, cycle_time_s, deprecated)
            VALUES (9004, 9001, 'Test Smelter', 60, FALSE) ON CONFLICT (id) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO resource_flows
                (parent_type, parent_id, recipe_id, item_id, direction, qty_per_cycle, qty_per_min, sort_order)
            VALUES (0, 9004, 9004, 9002, 0, 4, 4.0, 0),
                   (0, 9004, 9004, 9003, 1, 1, 1.0, 0)
            ON CONFLICT (parent_type, parent_id, item_id, direction) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO population_needs
                (item, label, need_type, qty_per_10k, unit, group_key, choice_group, is_default, sort_order)
            VALUES ('Test Iron Ore', 'ТестПресс', 1, 10, 'per_min', 'utility', NULL, TRUE, 9003)
            ON CONFLICT (label) DO NOTHING
        """)
    try:
        need = _find(client.get("/api/population/data").get_json(), "ТестПресс")
        ids = [o["recipe_id"] for o in need["options"]]
        assert 9004 not in ids, "вариант, лишь переупаковывающий отход, должен отсеиваться"
        assert 9001 in ids
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM population_needs WHERE label = 'ТестПресс'")
            cur.execute("DELETE FROM resource_flows WHERE recipe_id = 9004")
            cur.execute("DELETE FROM recipes WHERE id = 9004")
            cur.execute("DELETE FROM items WHERE id = 9003")


def test_medicine_tree_covers_tiers_and_components(client):
    """Медикаменты разворачиваются в дерево: сами уровни и их составляющие.

    Сырьё для медикаментов больше нигде не используется, поэтому его
    производство считаем явно. Глубже составляющих не идём — там уже
    общепромышленные материалы.
    """
    data = client.get("/api/population/data").get_json()
    need = next((n for n in data["needs"] if n["item"] == "Medical Supplies III"), None)
    if need is None:
        import pytest as _p
        _p.skip("в базе нет Medical Supplies III (чистая схема без игровых данных)")

    by_item = {n["item"]: n for n in need["tree"]}
    # оба младших уровня лекарств
    assert by_item["Medical Supplies II"]["parent"] == "Medical Supplies III"
    assert by_item["Medical Supplies"]["parent"] == "Medical Supplies II"
    assert by_item["Medical Supplies II"]["is_medicine"] is True
    # их составляющие
    for comp, parent in [("Anesthetics", "Medical Supplies III"),
                         ("Morphine",    "Medical Supplies III"),
                         ("Antibiotics", "Medical Supplies II"),
                         ("Disinfectant", "Medical Supplies"),
                         ("Medical Equipment", "Medical Supplies")]:
        assert comp in by_item, f"{comp} должен быть в дереве"
        assert by_item[comp]["parent"] == parent
        assert by_item[comp]["is_medicine"] is False
        assert by_item[comp]["options"], f"у {comp} должен быть рецепт"
    # глубже не разворачиваем: сталь и пластик — общепромышленное сырьё
    assert "Steel" not in by_item
    assert "Plastic" not in by_item


def test_preferred_defaults_are_marked(client):
    """Выбор по умолчанию задан правилом (здание + обязательный вход), а не id."""
    data = client.get("/api/population/data").get_json()
    wanted = {
        "Waste Water": ("Wastewater Treatment", "Filter Media"),
        "Waste":       ("Incineration Plant",   "Hydrogen"),
        "Electricity": ("Power Generator (Large)", None),
        "Computing":   ("Data Center",            None),
    }
    for need in data["needs"]:
        if need["item"] not in wanted:
            continue
        building, required_input = wanted[need["item"]]
        pref = [o for o in need["options"] if o.get("preferred")]
        if not need["options"]:
            continue
        assert len(pref) == 1, f'{need["item"]}: ожидался ровно один вариант по умолчанию'
        assert pref[0]["building"] == building
        if required_input:
            assert any(i["item"] == required_input for i in pref[0]["inputs"])


def test_building_families_are_reported(client):
    """Семейства с уровнями отдаются отдельно — уровни выбираются независимо."""
    data = client.get("/api/population/data").get_json()
    fams = {f["family"]: f for f in data["families"]}
    if not fams:
        import pytest as _p
        _p.skip("нет игровых данных с уровнями зданий")
    for f in fams.values():
        assert len(f["tiers"]) > 1
        assert f["max"] == max(t["tier"] for t in f["tiers"])
        # имя берётся от старшего уровня без римской цифры
        assert not f["display"].endswith((" I", " II", " III", " IV", " V"))


@pytest.fixture
def seed_tiered_building(db_conn, seed_game_data):
    """Семейство из двух уровней: у младшего рецепт помечен deprecated.

    В этих данных deprecated означает «вытеснено старшим уровнем», а не
    «убрано из игры», поэтому такие рецепты нужны как варианты уровней.
    """
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO buildings (id, name, workers, electricity_kw) VALUES
                (9010, 'Test Press',    4, 100),
                (9011, 'Test Press II', 6, 200),
                (9012, 'Test Legacy',   2,  50)
            ON CONFLICT (id) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO items (id, name) VALUES (9010, 'Test Widget') ON CONFLICT (id) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO recipes (id, machine_id, machine_name, cycle_time_s, deprecated) VALUES
                (9010, 9010, 'Test Press',    60, TRUE),
                (9011, 9011, 'Test Press II', 30, FALSE),
                (9012, 9012, 'Test Legacy',   60, TRUE)
            ON CONFLICT (id) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO resource_flows
                (parent_type, parent_id, recipe_id, item_id, direction, qty_per_cycle, qty_per_min, sort_order)
            VALUES (0, 9010, 9010, 9010, 1, 5, 5.0, 0),
                   (0, 9011, 9011, 9010, 1, 5, 10.0, 0),
                   (0, 9012, 9012, 9010, 1, 5, 1.0, 0)
            ON CONFLICT (parent_type, parent_id, item_id, direction) DO NOTHING
        """)
        cur.execute("""
            INSERT INTO population_needs
                (item, label, need_type, qty_per_10k, unit, group_key, choice_group, is_default, sort_order)
            VALUES ('Test Widget', 'ТестУровни', 0, 20, 'per_min', 'goods', NULL, TRUE, 9010)
            ON CONFLICT (label) DO NOTHING
        """)
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM population_needs WHERE label = 'ТестУровни'")
        cur.execute("DELETE FROM resource_flows WHERE recipe_id IN (9010, 9011, 9012)")
        cur.execute("DELETE FROM recipes WHERE id IN (9010, 9011, 9012)")
        cur.execute("DELETE FROM items WHERE id = 9010")
        cur.execute("DELETE FROM buildings WHERE id IN (9010, 9011, 9012)")


def test_lower_tier_recipes_are_offered(client, seed_tiered_building):
    need = _find(client.get("/api/population/data").get_json(), "ТестУровни")
    by_id = {o["recipe_id"]: o for o in need["options"]}
    assert 9011 in by_id, "верхний уровень должен быть в вариантах"
    assert 9010 in by_id, "младший уровень семейства должен быть в вариантах"
    assert by_id[9011]["tier"] == 2
    assert by_id[9010]["tier"] == 1
    assert by_id[9010]["family"] == by_id[9011]["family"] == "Test Press"


def test_legacy_recipe_without_higher_tier_is_excluded(client, seed_tiered_building):
    """Deprecated-рецепт у здания без старших уровней — настоящий легаси."""
    need = _find(client.get("/api/population/data").get_json(), "ТестУровни")
    assert 9012 not in [o["recipe_id"] for o in need["options"]]


def test_highest_tier_comes_first(client, seed_tiered_building):
    """Порядок задаёт выбор по умолчанию: старший уровень всегда быстрее."""
    need = _find(client.get("/api/population/data").get_json(), "ТестУровни")
    assert need["options"][0]["recipe_id"] == 9011


def test_food_groups_are_exposed(client):
    """Группы питания и их размер нужны странице, чтобы перераспределять доли.

    Норма в qty_per_10k задана для полного состава группы, поэтому «полная»
    норма продукта (когда он один закрывает всю четверть) выводится из неё
    умножением на размер группы.
    """
    data = client.get("/api/population/data").get_json()
    food = [n for n in data["needs"] if n["group_key"] == "food"]
    if not food:
        import pytest as _p
        _p.skip("в базе нет строк питания")

    by_group = {}
    for n in food:
        assert n["food_group"] in (1, 2, 3, 4), f'{n["item"]}: группа не задана'
        by_group.setdefault(n["food_group"], []).append(n)

    assert sorted(by_group) == [1, 2, 3, 4]
    for g, items in by_group.items():
        for n in items:
            assert n["group_size"] == len(items), (
                f'{n["item"]}: group_size {n["group_size"]} != {len(items)}'
            )

    # Полные нормы — те самые круглые числа, по которым модель и проверялась.
    # Сравниваем приблизительно: qty_per_10k хранится как NUMERIC(12,4), и
    # хлеб (одна треть от 50) округляется до 16,6667, давая в произведении
    # 50,0001. На расчёт это не влияет.
    rel = 1e-4
    full = {n["item"]: n["qty_per_10k"] * n["group_size"] for n in food}
    assert full["Potato"]     == pytest.approx(105,   rel=rel)
    assert full["Corn"]       == pytest.approx(75,    rel=rel)
    assert full["Bread"]      == pytest.approx(50,    rel=rel)
    assert full["Vegetables"] == pytest.approx(105,   rel=rel)
    assert full["Fruit"]      == pytest.approx(78.75, rel=rel)
    assert full["Tofu"]       == pytest.approx(45,    rel=rel)


def test_food_is_no_longer_hidden(client, db_conn):
    """Группа food когда-то пряталась из выдачи. Проверяем, что не прячется.

    Своя строка, а не реальные данные: на чистой схеме (как в CI) таблица
    потребностей пуста, и проверка по живым данным там ничего не значит.
    """
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO population_needs
                (item, label, need_type, qty_per_10k, unit, group_key, food_group, sort_order)
            VALUES (NULL, 'ТестЕда', 0, 10, 'per_min', 'food', 1, 9001)
            ON CONFLICT (label) DO NOTHING
        """)
    try:
        data = client.get("/api/population/data").get_json()
        assert any(n["label"] == "ТестЕда" and n["group_key"] == "food"
                   for n in data["needs"])
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM population_needs WHERE label = 'ТестЕда'")


def test_edicts_and_focuses_catalogs(client):
    """Каталоги указов и фокусов Офиса приходят целиком.

    Проверяем по статическим каталогам, а не по живым данным: на чистой
    схеме в CI игровые таблицы пусты, а эти константы живут в коде.
    """
    data = client.get("/api/population/data").get_json()

    edicts = {e["key"]: e for e in data["edicts"]}
    assert "food_saver" in edicts
    assert "plenty_of_food" in edicts
    # «Изобилие еды» Единство производит, а не тратит: по +1 за уровень.
    assert edicts["plenty_of_food"].get("unity_gain") == 1
    # Указы «Больше …» поднимают Единство своей услуги на 15% за уровень.
    assert edicts["more_goods"].get("unity_bonus") == 0.15

    focuses = {f["key"]: f for f in data["focuses"]}
    assert set(focuses) == {"focus_food", "focus_goods", "focus_crop", "focus_unity"}
    ff = focuses["focus_food"]
    assert ff["levels"] == 15
    assert ff["per_level"] == -0.02
    assert ff["initial"] == 75 and ff["increment"] == 100
    fc = focuses["focus_crop"]
    assert fc["levels"] == 150 and fc["per_level"] == 0.02
    assert fc["initial"] == 50 and fc["increment"] == 10
