"""
Конфигурация Playwright E2E-тестов.

Требует запущенного сервера — по умолчанию localhost:5001.
Переопределить: pytest tests/e2e/ --base-url http://host:port

Если сервер недоступен — вся сессия пропускается (skip).
"""
import pytest
import requests


SERVER = "http://localhost:5001"


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
    """Пропускаем всю E2E-сессию если сервер не запущен."""
    try:
        requests.get(base_url, timeout=3)
    except Exception:
        pytest.skip(
            f"E2E tests skipped: server not available at {base_url}. "
            "Start the app first: flask run --port 5001",
            allow_module_level=True,
        )


@pytest.fixture
def page(page, base_url):
    """
    Переопределяем стандартный playwright-фикстур:
    устанавливаем таймаут навигации 15 с (разумно для dev-сервера).
    """
    page.set_default_navigation_timeout(15_000)
    page.set_default_timeout(8_000)
    yield page
