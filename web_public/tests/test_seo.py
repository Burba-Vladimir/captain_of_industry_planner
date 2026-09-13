"""
Тесты SEO-слоя: серверные страницы справочника, robots.txt и sitemap.xml.

Смысл этих страниц — быть видимыми без JS, поэтому проверяем именно то,
что увидит краулер: статус, заголовки, canonical/hreflang и наличие
осмысленного текста в HTML.
"""
import re
import urllib.parse

import pytest

from seo import slugify

BOT = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"}


def _text(resp) -> str:
    """Видимый текст страницы — без тегов, как его увидит поисковик."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", resp.data.decode())).strip()


# Сущности игры, на которых проверяются страницы справочника.
_REF_ITEMS = ("Ammonia", "Sour Water")
_REF_BUILDINGS = ("Alloy Mixer", "Chemical Plant II")


@pytest.fixture
def game_data(db_conn):
    """Пропускает тест, если в базе нет настоящих данных игры.

    CI поднимает базу только из schema.sql: там есть лишь тестовые строки из
    seed_game_data, а ни Ammonia, ни Alloy Mixer нет. Смотрим в саму базу, а не
    на ответ страницы — иначе сломанная страница на полной базе тихо
    пропускалась бы, вместо того чтобы упасть.
    """
    with db_conn.cursor() as cur:
        cur.execute("SELECT name FROM items WHERE name = ANY(%s)", (list(_REF_ITEMS),))
        found = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT name FROM buildings WHERE name = ANY(%s)", (list(_REF_BUILDINGS),))
        found |= {r[0] for r in cur.fetchall()}
    missing = sorted(set(_REF_ITEMS + _REF_BUILDINGS) - found)
    if missing:
        pytest.skip("нет игровых данных (чистая схема): " + ", ".join(missing))


class TestSlugify:
    @pytest.mark.parametrize("name,expected", [
        ("Ammonia",             "ammonia"),
        ("Steam (High)",        "steam-high"),
        ("Fertilizer I",        "fertilizer-i"),
        ("Assembly (Electric)", "assembly-electric"),
    ])
    def test_slugify(self, name, expected):
        assert slugify(name) == expected


class TestRobots:
    def test_robots_ok(self, client):
        r = client.get("/robots.txt")
        assert r.status_code == 200
        assert r.mimetype == "text/plain"

    def test_robots_blocks_api_and_names_sitemap(self, client):
        body = client.get("/robots.txt").data.decode()
        assert "Disallow: /api/" in body
        assert "Disallow: /complex/*/edit" in body
        assert "Sitemap:" in body and "/sitemap.xml" in body


class TestSitemap:
    def test_sitemap_ok(self, client):
        r = client.get("/sitemap.xml")
        assert r.status_code == 200
        assert r.mimetype == "application/xml"

    @pytest.mark.usefixtures("game_data")
    def test_sitemap_lists_reference_pages(self, client):
        body = client.get("/sitemap.xml").data.decode()
        assert body.count("<loc>") > 100          # сотни страниц справочника
        assert "/items" in body and "/buildings" in body
        assert "/item/" in body and "/building/" in body

    def test_sitemap_has_hreflang_alternates(self, client):
        body = client.get("/sitemap.xml").data.decode()
        assert 'hreflang="en"' in body
        assert 'hreflang="ru"' in body
        assert 'hreflang="x-default"' in body

    def test_every_reference_url_is_reachable(self, client):
        """В sitemap не должно быть URL, отдающих 404 — это прямой минус в индексе."""
        body = client.get("/sitemap.xml").data.decode()
        paths = [re.sub(r"^https?://[^/]+", "", u)
                 for u in re.findall(r"<loc>(.*?)</loc>", body)]
        checked = [p for p in paths if p.startswith(("/item/", "/building/"))]
        assert checked, "в sitemap нет страниц справочника"
        broken = [p for p in checked[:40] if client.get(p).status_code != 200]
        assert broken == []


class TestCatalogPages:
    @pytest.mark.usefixtures("game_data")
    @pytest.mark.parametrize("path", ["/items", "/buildings"])
    def test_catalog_ok(self, client, path):
        r = client.get(path)
        assert r.status_code == 200
        assert len(_text(r).split()) > 100

    def test_items_catalog_links_to_detail_pages(self, client):
        html = client.get("/items").data.decode()
        assert 'href="/item/' in html

    def test_buildings_catalog_links_to_detail_pages(self, client):
        html = client.get("/buildings").data.decode()
        assert 'href="/building/' in html


class TestItemPage:
    @pytest.mark.usefixtures("game_data")
    def test_known_item_ok(self, client):
        r = client.get("/item/ammonia")
        assert r.status_code == 200

    def test_unknown_item_404(self, client):
        r = client.get("/item/definitely-not-a-real-resource")
        assert r.status_code == 404

    @pytest.mark.usefixtures("game_data")
    def test_content_visible_without_js(self, client):
        """Главное свойство страницы: текст есть в HTML, а не рисуется скриптом."""
        text = _text(client.get("/item/ammonia", headers=BOT))
        assert len(text.split()) > 120
        assert "Ammonia" in text
        assert "Captain of Industry" in text

    @pytest.mark.usefixtures("game_data")
    def test_head_tags(self, client):
        html = client.get("/item/ammonia").data.decode()
        assert "<title>Ammonia" in html
        assert 'name="description"' in html
        assert 'rel="canonical"' in html
        assert 'hreflang="ru"' in html
        assert 'property="og:title"' in html

    @pytest.mark.usefixtures("game_data")
    def test_localized(self, client):
        html = client.get("/item/ammonia?lang=ru").data.decode()
        assert "Аммиак" in html
        # английское имя остаётся на странице — по нему тоже ищут
        assert "Ammonia" in html

    @pytest.mark.usefixtures("game_data")
    def test_slug_is_case_insensitive(self, client):
        assert client.get("/item/Ammonia").status_code == 200

    @pytest.mark.usefixtures("game_data")
    def test_links_to_related_pages(self, client):
        html = client.get("/item/ammonia").data.decode()
        assert 'href="/building/' in html    # машины из таблиц рецептов
        assert 'href="/item/' in html        # соседние ресурсы


@pytest.mark.usefixtures("game_data")
class TestPlannerHandoff:
    """Кнопка «Открыть в планировщике» должна вести на непустую выдачу.

    Поиск на главной сверяется с локализованными именами, поэтому английское имя
    в русской локали давало ноль результатов; а значение без кавычек обрывается
    на первом пробеле, из-за чего многословные имена искались как подстроки.
    """

    def _cta_query(self, client, path, lang):
        html = client.get(f"{path}?lang={lang}").data.decode()
        hrefs = re.findall(r'href="/\?q=([^"]+)"', html)
        assert hrefs, f"на {path} нет ссылки в планировщик"
        return urllib.parse.unquote(hrefs[-1])

    @pytest.mark.parametrize("path,expected_prefix", [
        ("/item/ammonia",              "out:"),
        ("/item/sour-water",           "out:"),      # два слова
        ("/building/alloy-mixer",      "name:"),
        ("/building/chemical-plant-ii", "name:"),    # три слова
    ])
    @pytest.mark.parametrize("lang", ["en", "ru"])
    def test_query_finds_something(self, client, path, expected_prefix, lang):
        q = self._cta_query(client, path, lang)
        assert q.startswith(expected_prefix)
        assert q.endswith('"'), "значение должно быть в кавычках"
        r = client.get(f"/api/nodes?q={urllib.parse.quote(q)}&per_page=100")
        assert r.status_code == 200
        assert r.get_json()["total"] > 0, f"{lang}: запрос {q} ничего не нашёл"

    def test_russian_query_uses_localized_name(self, client):
        q = self._cta_query(client, "/item/ammonia", "ru")
        assert "Аммиак" in q
        assert "Ammonia" not in q


class TestBuildingPage:
    @pytest.mark.usefixtures("game_data")
    def test_known_building_ok(self, client):
        r = client.get("/building/alloy-mixer")
        assert r.status_code == 200

    def test_unknown_building_404(self, client):
        assert client.get("/building/not-a-building").status_code == 404

    @pytest.mark.usefixtures("game_data")
    def test_shows_stats_and_recipes(self, client):
        text = _text(client.get("/building/alloy-mixer", headers=BOT))
        assert "Alloy Mixer" in text
        assert len(text.split()) > 100


class TestLanding:
    """Главная — SPA, но заголовок и описание должны быть в HTML без JS."""

    def test_h1_and_lead_rendered_server_side(self, client):
        text = _text(client.get("/", headers=BOT))
        assert "Captain of Industry production planner" in text
        assert "reusable block" in text

    def test_counts_are_numbers_not_repr(self, client):
        """counts.items в Jinja вернул бы метод dict — ловим это как регрессию."""
        text = _text(client.get("/", headers=BOT))
        assert "built-in method" not in text
        assert re.search(r"\d+ recipes, \d+ buildings and \d+ resources", text)

    def test_links_to_reference_sections(self, client):
        html = client.get("/").data.decode()
        assert 'href="/items"' in html
        assert 'href="/buildings"' in html

    def test_localized(self, client):
        text = _text(client.get("/?lang=ru", headers=BOT))
        assert "Планировщик производства" in text


class TestCrawlerHandling:
    @pytest.mark.parametrize("ua", [
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
        "Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)",
        "facebookexternalhit/1.1",
        "Twitterbot/1.0",
        "curl/8.4.0",
    ])
    def test_bots_detected(self, app, ua):
        from app import _is_crawler
        with app.test_request_context(headers={"User-Agent": ua}):
            assert _is_crawler()

    @pytest.mark.parametrize("ua", [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        # «bot» внутри модели телефона не должно считаться ботом
        "Mozilla/5.0 (Linux; Android 10; CUBOT_X30) AppleWebKit/537.36 Chrome/119 Mobile Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    ])
    def test_humans_not_detected(self, app, ua):
        from app import _is_crawler
        with app.test_request_context(headers={"User-Agent": ua}):
            assert not _is_crawler()

    @pytest.mark.usefixtures("game_data")
    def test_crawler_gets_no_guest_cookie(self, app):
        """Бот не хранит cookie: заводить ему гостя — плодить мусор в users."""
        with app.test_client() as c:
            r = c.get("/item/ammonia", headers=BOT)
        assert r.status_code == 200
        assert "coi_guest" not in r.headers.get("Set-Cookie", "")

    def test_human_still_gets_guest_cookie(self, app):
        ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
        with app.test_client() as c:
            r = c.get("/item/ammonia", headers=ua)
        assert "coi_guest" in r.headers.get("Set-Cookie", "")
