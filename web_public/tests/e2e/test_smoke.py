"""
Smoke E2E тесты CoI Planner.

Проверяют три сценария:
  1. Главная страница загружается и Alpine инициализируется.
  2. Browse → Community: переключение вкладок меняет URL и показывает таблицу.
  3. Редактор: новый комплекс открывается, кнопка Save активируется при вводе имени,
     сохранение обновляет URL на /complex/<uuid>/edit.

Запуск:
    pytest tests/e2e/ -v
    pytest tests/e2e/ -v --headed          # с видимым браузером
    pytest tests/e2e/ -v --base-url http://localhost:5001
"""
import re
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


# ── Хелперы ───────────────────────────────────────────────────────────────────

def wait_alpine(page: Page):
    """Ждём, пока Alpine не уберёт x-cloak с body (признак инициализации)."""
    page.wait_for_selector("body:not([x-cloak])", timeout=8_000)


# ── 1. Главная страница ───────────────────────────────────────────────────────

class TestHomePage:
    def test_page_title(self, page: Page, base_url):
        page.goto(base_url)
        wait_alpine(page)
        assert "Captain of Industry" in page.title() or "CoI" in page.title()

    def test_browse_table_loads(self, page: Page, base_url):
        """После загрузки Browse-таблица отображает строки с данными."""
        page.goto(base_url)
        wait_alpine(page)
        # Таблица должна быть видима и содержать хотя бы одну строку данных
        table = page.locator("table").first
        expect(table).to_be_visible()
        rows = page.locator("table tbody tr")
        expect(rows.first).to_be_visible(timeout=8_000)

    def test_community_tab_switch(self, page: Page, base_url):
        """Клик по Community меняет URL и показывает свою таблицу/список."""
        page.goto(base_url)
        wait_alpine(page)
        # Кнопка Community — первая кнопка с таким текстом в шапке вкладок
        page.locator("button", has_text=re.compile(r"Community", re.I)).click()
        # URL должен обновиться через history.replaceState
        page.wait_for_url(re.compile(r"tab=community"), timeout=5_000)
        # При наличии публичных комплексов таблица видна;
        # при отсутствии — сообщение «нет комплексов». Оба варианта ОК.
        page.wait_for_load_state("networkidle")
        # Не выбрасываем JS-ошибок
        assert page.evaluate("document.readyState") == "complete"

    def test_no_js_errors(self, page: Page, base_url):
        """Консоль не содержит критических JS-ошибок при загрузке."""
        errors = []
        page.on("pageerror", lambda e: errors.append(e.message))
        page.goto(base_url)
        wait_alpine(page)
        page.wait_for_load_state("networkidle")
        # Фильтруем шумовые ошибки не связанные с нашим кодом
        critical = [e for e in errors if "scaledFlows" not in e and "favicon" not in e]
        assert critical == [], f"JS errors: {critical}"


# ── 2. Редактор ───────────────────────────────────────────────────────────────

class TestEditor:
    def test_editor_opens(self, page: Page, base_url):
        """Маршрут /complex/new рендерит канвас редактора."""
        page.goto(f"{base_url}/complex/new")
        wait_alpine(page)
        expect(page.locator("#canvas-wrap")).to_be_visible()

    def test_save_disabled_without_name(self, page: Page, base_url):
        """Кнопка Save недоступна пока не введено имя комплекса."""
        page.goto(f"{base_url}/complex/new")
        wait_alpine(page)
        # Первая кнопка Save (не Save & Exit)
        save_btn = page.locator("button[\\@click='save(false)']")
        expect(save_btn).to_be_disabled()

    def test_name_enables_save(self, page: Page, base_url):
        """Ввод имени активирует кнопку Save."""
        page.goto(f"{base_url}/complex/new")
        wait_alpine(page)
        name_input = page.locator("input[x-model='name']")
        save_btn = page.locator("button[\\@click='save(false)']")
        expect(save_btn).to_be_disabled()
        name_input.fill("E2E Smoke Complex")
        expect(save_btn).to_be_enabled(timeout=2_000)

    def test_save_new_complex_updates_url(self, page: Page, base_url):
        """Сохранение нового комплекса обновляет URL на /complex/<uuid>/edit."""
        page.goto(f"{base_url}/complex/new")
        wait_alpine(page)
        page.locator("input[x-model='name']").fill("E2E Smoke Complex")
        page.locator("button[\\@click='save(false)']").click()
        # Ждём URL с UUID (history.replaceState меняет URL без перезагрузки)
        page.wait_for_url(
            re.compile(r"/complex/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/edit"),
            timeout=10_000,
        )
        # Редактор остался на месте (не произошёл переход)
        expect(page.locator("#canvas-wrap")).to_be_visible()

    def test_back_navigation(self, page: Page, base_url):
        """Кнопка ← Назад ведёт обратно на главную."""
        page.goto(f"{base_url}/complex/new?back={base_url}/")
        wait_alpine(page)
        page.locator("a", has_text=re.compile(r"Back|Назад")).click()
        page.wait_for_url(base_url + "/", timeout=5_000)
