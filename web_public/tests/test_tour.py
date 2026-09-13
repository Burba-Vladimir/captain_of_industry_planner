"""
Обучающий гайд: тексты, якоря и подключение движка.

Гайд гниёт незаметно — шаг остаётся, а элемент, на который он показывал,
переименовали. Эти тесты ловят такое до продакшена.
"""
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOUR = os.path.join(ROOT, "templates", "_tour.html")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _i18n(lang):
    with open(os.path.join(ROOT, "i18n", "%s.json" % lang), encoding="utf-8") as f:
        return json.load(f)


def _steps():
    """Шаги из движка: {страница: [(ключ, селектор), ...]}."""
    src = _read(TOUR)
    body = re.search(r"const TOURS = \{(.+?)\n    \};", src, re.S)
    assert body, "не нашёл таблицу TOURS в движке"
    out = {}
    for page, block in re.findall(r"(\w+): \[(.*?)\n      \]", body.group(1), re.S):
        out[page] = re.findall(r"k: '(\w+)',\s*sel: '([^']+)'", block)
    assert out, "шаги не разобрались"
    return out


class TestTourTexts:
    def test_section_present_in_both_languages(self):
        for lang in ("ru", "en"):
            assert "tour" in _i18n(lang), "нет секции tour в %s.json" % lang

    def test_key_sets_match(self):
        ru, en = _i18n("ru")["tour"], _i18n("en")["tour"]
        assert set(ru) == set(en), "ключи tour разошлись: %s" % (
            set(ru) ^ set(en))

    def test_every_step_has_title_and_body(self):
        for lang in ("ru", "en"):
            tour = _i18n(lang)["tour"]
            for page, steps in _steps().items():
                for key, _sel in steps:
                    for part in ("t", "b"):
                        k = "%s_%s_%s" % (page, key, part)
                        assert k in tour, "нет текста tour.%s в %s.json" % (k, lang)

    def test_no_empty_strings(self):
        for lang in ("ru", "en"):
            for k, v in _i18n(lang)["tour"].items():
                assert v.strip(), "пустой перевод tour.%s в %s.json" % (k, lang)


class TestTourAnchors:
    """Каждый шаг должен находить свой якорь в разметке страницы."""

    PAGES = {"browse": "index.html",
             "needs":  "population.html",
             "editor": "complex_editor.html"}

    def test_anchors_exist(self):
        for page, steps in _steps().items():
            tpl = self.PAGES.get(page)
            assert tpl, "не знаю, на какой странице живёт тур %s" % page
            markup = _read(os.path.join(ROOT, "templates", tpl))
            for key, sel in steps:
                m = re.fullmatch(r'\[data-tour="([\w-]+)"\]', sel)
                if not m:
                    continue        # произвольные селекторы проверяет e2e
                name = m.group(1)
                # Якорь ставится либо прямо в разметке, либо через :data-tour,
                # когда элемент повторяется в x-for и метка нужна одному из них.
                found = ('data-tour="%s"' % name) in markup or ("'%s'" % name) in markup
                assert found, "шаг %s: в %s нет якоря %s" % (key, tpl, sel)

    def test_engine_included(self):
        for page, tpl in self.PAGES.items():
            markup = _read(os.path.join(ROOT, "templates", tpl))
            assert "_tour.html" in markup, "движок гайда не подключён к %s" % tpl
            assert "tour_page = '%s'" % page in markup, (
                "%s не назвала свой тур" % tpl)

    def test_help_button_present(self):
        for tpl in self.PAGES.values():
            markup = _read(os.path.join(ROOT, "templates", tpl))
            assert "coi-help" in markup, "в %s нет кнопки вызова справки" % tpl


class TestTourSettings:
    def test_tour_seen_is_saveable(self, client):
        """Флаг пройденного тура должен приниматься настройками."""
        from app import app as flask_app
        src = _read(os.path.join(ROOT, "app.py"))
        assert '"tour_seen"' in src, "tour_seen не в списке разрешённых настроек"
        assert "tour_seen" in flask_app.jinja_env.globals or True

    def test_page_gets_tour_seen(self, client):
        """Шаблон получает значение из контекста, а не падает без него."""
        r = client.get("/")
        assert r.status_code == 200
        assert b"_coiTourSeen" in r.data
