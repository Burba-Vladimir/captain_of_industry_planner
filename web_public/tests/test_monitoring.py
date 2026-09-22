"""
Тесты мониторинга: проверки, доступ к админке и запись суточных срезов.

Синтетические срезы пишутся на даты 2000 года — так они гарантированно не
пересекаются с настоящими данными стенда и вычищаются после теста.
"""
import datetime as _dt

import pytest

import monitoring
from db import get_db

# Заведомо «чужие» сутки: настоящих срезов за них быть не может
D0 = _dt.date(2000, 1, 10)
D1 = _dt.date(2000, 1, 11)
D2 = _dt.date(2000, 1, 12)

GB = 2 ** 30


@pytest.fixture
def clean_synthetic():
    """Убирает синтетические строки до и после теста."""
    def wipe():
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute("DELETE FROM daily_stats WHERE day < '2001-01-01'")
                cur.execute("DELETE FROM monitor_alerts WHERE day < '2001-01-01'")
            con.commit()
    wipe()
    yield
    wipe()


@pytest.fixture
def admin_user(db_conn):
    """Пользователь с почтой, внесённой в ADMIN_EMAILS."""
    email = "monitoring-test-admin@example.com"
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO users (provider, provider_user_id, display_name, email, is_guest)
            VALUES ('email', %s, 'Test Admin', %s, FALSE)
            ON CONFLICT (provider, provider_user_id) DO UPDATE SET email = EXCLUDED.email
            RETURNING id
        """, (email, email))
        uid = cur.fetchone()[0]
    yield {"id": uid, "email": email}
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (uid,))


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess["user_id"] = user_id


# ─────────────────────────────────────────────────────────────────
# Проверки — чистая функция, БД не нужна
# ─────────────────────────────────────────────────────────────────

def _snapshot(**over):
    base = {
        "day": D2,
        "new_guests": 50,
        "db_size_bytes": 100 * 2 ** 20,
        "disk_free_bytes": 50 * GB,
        "disk_total_bytes": 100 * GB,
    }
    base.update(over)
    return base


def _history(days=7, new_guests=50, db_size=100 * 2 ** 20):
    return [{"day": D2 - _dt.timedelta(days=i + 1),
             "new_guests": new_guests,
             "db_size_bytes": db_size} for i in range(days)]


class TestChecks:
    def test_quiet_when_everything_normal(self):
        assert monitoring.evaluate_checks(_snapshot(), _history()) == []

    def test_disk_low(self):
        alerts = monitoring.evaluate_checks(
            _snapshot(disk_free_bytes=5 * GB), _history())
        kinds = {a["kind"] for a in alerts}
        assert "disk_low" in kinds

    def test_disk_critical_below_five_percent(self):
        alerts = monitoring.evaluate_checks(
            _snapshot(disk_free_bytes=2 * GB), _history())
        disk = next(a for a in alerts if a["kind"] == "disk_low")
        assert disk["severity"] == "critical"

    def test_db_growth(self):
        alerts = monitoring.evaluate_checks(
            _snapshot(db_size_bytes=200 * 2 ** 20), _history())
        assert "db_growth" in {a["kind"] for a in alerts}

    def test_no_traffic_is_critical(self):
        alerts = monitoring.evaluate_checks(_snapshot(new_guests=0), _history())
        no_traffic = next(a for a in alerts if a["kind"] == "no_traffic")
        assert no_traffic["severity"] == "critical"

    def test_traffic_drop(self):
        alerts = monitoring.evaluate_checks(_snapshot(new_guests=5), _history())
        assert "traffic_drop" in {a["kind"] for a in alerts}

    def test_small_numbers_do_not_trigger_traffic_drop(self):
        """При медиане 3 падение до 1 — шум, а не авария."""
        alerts = monitoring.evaluate_checks(
            _snapshot(new_guests=1), _history(new_guests=3))
        assert "traffic_drop" not in {a["kind"] for a in alerts}

    def test_zero_traffic_on_brand_new_site_is_quiet(self):
        """Истории нет — жаловаться не на что."""
        assert monitoring.evaluate_checks(_snapshot(new_guests=0), []) == []

    def test_collector_gap(self):
        history = [{"day": D2 - _dt.timedelta(days=4),
                    "new_guests": 50, "db_size_bytes": 100 * 2 ** 20}]
        alerts = monitoring.evaluate_checks(_snapshot(), history)
        assert "collector_gap" in {a["kind"] for a in alerts}

    def test_no_gap_on_consecutive_days(self):
        alerts = monitoring.evaluate_checks(_snapshot(), _history())
        assert "collector_gap" not in {a["kind"] for a in alerts}

    def test_missing_health_data_is_not_an_alert(self):
        """У восстановленных суток полей здоровья нет — это не повод тревожить."""
        snap = _snapshot(disk_free_bytes=None, disk_total_bytes=None, db_size_bytes=None)
        alerts = monitoring.evaluate_checks(snap, _history())
        assert {a["kind"] for a in alerts} & {"disk_low", "db_growth"} == set()


# ─────────────────────────────────────────────────────────────────
# Запись срезов
# ─────────────────────────────────────────────────────────────────

class TestSnapshots:
    def _write(self, day, **fields):
        row = {c: None for c in monitoring._UPSERT_COLUMNS}
        row.update(fields)
        with get_db() as con:
            with con.cursor() as cur:
                monitoring._upsert(cur, day, row, {}, backfilled=fields.pop("_bf", False))
            con.commit()

    def _read(self, day):
        with get_db() as con:
            with con.cursor() as cur:
                cur.execute("""
                    SELECT new_guests, db_size_bytes, is_backfilled
                    FROM daily_stats WHERE day = %s
                """, (day,))
                return cur.fetchone()

    def test_upsert_writes_row(self, clean_synthetic):
        self._write(D1, new_guests=42)
        assert self._read(D1)[0] == 42

    def test_repeat_collection_overwrites(self, clean_synthetic):
        self._write(D1, new_guests=42)
        self._write(D1, new_guests=99)
        assert self._read(D1)[0] == 99

    def test_backfill_does_not_erase_measured_health(self, clean_synthetic):
        """Повторный backfill не должен затирать живой сбор пустыми полями."""
        self._write(D1, new_guests=42, db_size_bytes=12345)
        row = {c: None for c in monitoring._UPSERT_COLUMNS}
        row["new_guests"] = 42
        with get_db() as con:
            with con.cursor() as cur:
                monitoring._upsert(cur, D1, row, {}, backfilled=True)
            con.commit()
        new_guests, db_size, is_backfilled = self._read(D1)
        assert db_size == 12345, "backfill затёр измеренный размер БД"
        assert is_backfilled is False, "живой срез не должен становиться восстановленным"

    def test_dry_run_writes_nothing(self, clean_synthetic):
        before = monitoring.collect_day(D0, dry_run=True)
        assert before["day"] == D0
        assert self._read(D0) is None

    def test_collect_day_returns_health(self):
        row = monitoring.collect_day(monitoring.today_utc(), dry_run=True)
        assert row["db_size_bytes"] > 0
        assert row["disk_total_bytes"] > 0
        assert row["collect_ms"] >= 0


class TestAlertDedup:
    def test_same_alert_not_repeated_for_one_day(self, clean_synthetic):
        sent = []

        def fake_mail(subject, body):
            sent.append(subject)

        row = {c: None for c in monitoring._UPSERT_COLUMNS}
        row.update({"new_guests": 0, "disk_free_bytes": 1 * GB,
                    "disk_total_bytes": 100 * GB, "db_size_bytes": 100 * 2 ** 20})
        with get_db() as con:
            with con.cursor() as cur:
                for i in range(8):
                    prev = {c: None for c in monitoring._UPSERT_COLUMNS}
                    prev.update({"new_guests": 50, "db_size_bytes": 100 * 2 ** 20})
                    monitoring._upsert(cur, D2 - _dt.timedelta(days=i + 1), prev, {}, False)
                monitoring._upsert(cur, D2, row, {}, backfilled=False)
            con.commit()

        first = monitoring.run_checks(D2, send_email=fake_mail)
        second = monitoring.run_checks(D2, send_email=fake_mail)

        assert {a["kind"] for a in first} >= {"disk_low", "no_traffic"}
        assert second == [], "повторный прогон не должен слать письмо снова"
        assert len(sent) == 1

    def test_alert_marked_as_notified(self, clean_synthetic):
        row = {c: None for c in monitoring._UPSERT_COLUMNS}
        row.update({"new_guests": 0, "disk_free_bytes": 1 * GB,
                    "disk_total_bytes": 100 * GB})
        with get_db() as con:
            with con.cursor() as cur:
                prev = {c: None for c in monitoring._UPSERT_COLUMNS}
                prev.update({"new_guests": 50})
                monitoring._upsert(cur, D1, prev, {}, False)
                monitoring._upsert(cur, D2, row, {}, False)
            con.commit()

        monitoring.run_checks(D2, send_email=lambda s, b: None)

        with get_db() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM monitor_alerts WHERE day = %s AND notified_at IS NOT NULL",
                    (D2,))
                assert cur.fetchone()[0] > 0

    def test_sender_falls_back_when_smtp_from_is_blank(self, monkeypatch):
        """SMTP_FROM обычно есть в .env, но пустой — письмо не должно уходить без отправителя."""
        import smtplib
        from auth import send_mail

        sent = {}

        class FakeSMTP:
            def __init__(self, host, port): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def ehlo(self): pass
            def starttls(self): pass
            def login(self, u, p): pass
            def sendmail(self, frm, to, msg): sent["from"] = frm

        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("SMTP_USER", "robot@example.com")
        monkeypatch.setenv("SMTP_FROM", "")

        assert send_mail("to@example.com", "тема", "текст") is True
        assert sent["from"] == "robot@example.com"

    def test_failed_send_leaves_alert_unnotified(self, clean_synthetic):
        """Письмо не ушло — тревога записана, но не помечена отправленной."""
        def broken_mail(subject, body):
            raise RuntimeError("SMTP недоступен")

        row = {c: None for c in monitoring._UPSERT_COLUMNS}
        row.update({"new_guests": 0, "disk_free_bytes": 1 * GB, "disk_total_bytes": 100 * GB})
        with get_db() as con:
            with con.cursor() as cur:
                prev = {c: None for c in monitoring._UPSERT_COLUMNS}
                prev.update({"new_guests": 50})
                monitoring._upsert(cur, D1, prev, {}, False)
                monitoring._upsert(cur, D2, row, {}, False)
            con.commit()

        monitoring.run_checks(D2, send_email=broken_mail)

        with get_db() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM monitor_alerts WHERE day = %s AND notified_at IS NULL",
                    (D2,))
                assert cur.fetchone()[0] > 0


# ─────────────────────────────────────────────────────────────────
# Доступ к админке
# ─────────────────────────────────────────────────────────────────

class TestAdminAccess:
    """Роут отдаёт 404 всем, кроме админа: по 403 админку можно найти перебором."""

    @pytest.mark.parametrize("path", ["/admin/stats", "/admin/stats.json"])
    def test_anonymous_gets_404(self, client, path):
        assert client.get(path).status_code == 404

    @pytest.mark.parametrize("path", ["/admin/stats", "/admin/stats.json"])
    def test_logged_in_non_admin_gets_404(self, client, admin_user, path, monkeypatch):
        import app as A
        monkeypatch.setattr(A, "ADMIN_EMAILS", set())   # почты нет в списке
        _login(client, admin_user["id"])
        assert client.get(path).status_code == 404

    def test_admin_gets_dashboard(self, client, admin_user, monkeypatch):
        import app as A
        monkeypatch.setattr(A, "ADMIN_EMAILS", {admin_user["email"]})
        _login(client, admin_user["id"])
        r = client.get("/admin/stats")
        assert r.status_code == 200
        assert "Мониторинг".encode() in r.data

    def test_admin_gets_json(self, client, admin_user, monkeypatch):
        import app as A
        monkeypatch.setattr(A, "ADMIN_EMAILS", {admin_user["email"]})
        _login(client, admin_user["id"])
        payload = client.get("/admin/stats.json?days=7").get_json()
        assert "series" in payload and "live" in payload
        assert payload["window_days"] == 7

    def test_email_match_ignores_case_and_spaces(self, client, admin_user, monkeypatch):
        import app as A
        monkeypatch.setattr(A, "ADMIN_EMAILS", {admin_user["email"].upper()})
        _login(client, admin_user["id"])
        assert client.get("/admin/stats").status_code == 200

    def test_guest_is_never_admin(self, client, monkeypatch):
        """У гостя нет почты — он не должен проходить даже при пустом списке."""
        import app as A
        monkeypatch.setattr(A, "ADMIN_EMAILS", {""})
        assert client.get("/admin/stats").status_code == 404

    def test_dashboard_is_noindex(self, client, admin_user, monkeypatch):
        import app as A
        monkeypatch.setattr(A, "ADMIN_EMAILS", {admin_user["email"]})
        _login(client, admin_user["id"])
        assert b'name="robots" content="noindex' in client.get("/admin/stats").data

    def test_admin_not_in_sitemap_or_robots(self, client):
        sitemap = client.get("/sitemap.xml").data.decode()
        assert "/admin" not in sitemap
