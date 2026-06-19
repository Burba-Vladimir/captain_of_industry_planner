"""
Конфигурация Playwright E2E-тестов.

Требует запущенного сервера — по умолчанию localhost:5001.
Переопределить: pytest tests/e2e/ --base-url http://host:port

Если сервер недоступен — вся сессия пропускается (skip).
"""
import time

import pytest
import requests
from playwright.sync_api import expect

# Глобальный таймаут expect()-ассертов. По умолчанию Playwright даёт 5 с и НЕ
# учитывает page.set_default_timeout — поэтому задаём явно, с запасом под dev-сервер.
expect.set_options(timeout=10_000)


SERVER = "http://localhost:5001"

# Сколько ждём готовности сервера. Dev-сервер в debug-режиме (watchdog-reloader)
# на холодную может отвечать на первый запрос в разы дольше обычного.
_READY_TIMEOUT_S = 30
_READY_POLL_S    = 0.5


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "e2e: end-to-end browser test (requires running server)",
    )


@pytest.fixture(scope="session")
def base_url(pytestconfig):
    """URL сервера — берётся из --base-url или SERVER по умолчанию."""
    return pytestconfig.getoption("--base-url", default=SERVER) or SERVER


@pytest.fixture(scope="session", autouse=True)
def require_server(base_url):
    """Пропускаем всю E2E-сессию если сервер не запущен, иначе прогреваем его.

    Прогрев устраняет флак холодного старта: первый запрос к dev-серверу
    компилирует Jinja-шаблоны и инициализирует пул БД, из-за чего первый же
    реальный тест может не уложиться в таймаут видимости. Дёргаем главную и
    /api/nodes заранее, после готовности сервера.
    """
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last_err = None
    while time.monotonic() < deadline:
        try:
            requests.get(base_url, timeout=5)
            break
        except Exception as e:  # сервер ещё поднимается
            last_err = e
            time.sleep(_READY_POLL_S)
    else:
        pytest.skip(
            f"E2E tests skipped: server not available at {base_url} "
            f"({last_err}). Start the app first (without reloader): "
            "flask run --port 5001 --no-reload",
            allow_module_level=True,
        )

    # Прогрев: тёплый путь рендера таблицы Browse (главная + данные).
    try:
        requests.get(base_url, timeout=10)
        requests.get(
            f"{base_url.rstrip('/')}/api/nodes?page=1&per_page=50&type=all&hidden=false",
            timeout=10,
        )
    except Exception:
        pass  # прогрев best-effort: тесты сами разберутся


@pytest.fixture
def page(page, base_url):
    """
    Переопределяем стандартный playwright-фикстур:
    устанавливаем таймаут навигации 15 с (разумно для dev-сервера).
    """
    page.set_default_navigation_timeout(15_000)
    page.set_default_timeout(8_000)
    yield page
