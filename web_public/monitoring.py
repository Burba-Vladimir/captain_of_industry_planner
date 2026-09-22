"""
Мониторинг активности: суточные срезы, проверки и данные для админ-дашборда.

Устройство:

    collect_day(day)      считает срез за сутки и кладёт в daily_stats
    backfill(days)        восстанавливает прошлые сутки из created_at
    run_checks(day)       сверяет срез с историей, пишет в monitor_alerts
    dashboard_data(days)  всё, что рисует /admin/stats

Сутки считаются по UTC: сервер и БД живут в UTC, а граница «дня» должна быть
одинаковой при каждом запуске, иначе соседние срезы перекрываются.

Что НЕ восстанавливается задним числом и потому собирается только вживую:
размер БД, свободное место, число соединений и активность (last_seen_at хранит
одно текущее значение на пользователя, истории у него нет).
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shutil
import time

from db import dict_cursor, get_db

log = logging.getLogger(__name__)

# Сколько суток показывать на дашборде по умолчанию
DEFAULT_WINDOW_DAYS = 30

# ─────────────────────────────────────────────────────────────────
# Пороги проверок. Вынесены в окружение: на разных стендах разные.
# ─────────────────────────────────────────────────────────────────

# Тревога, если свободного места на диске меньше этой доли
DISK_FREE_MIN_RATIO = float(os.environ.get("MONITOR_DISK_MIN_RATIO", "0.15"))
# Тревога, если БД за сутки выросла больше чем на эту долю
DB_GROWTH_MAX_RATIO = float(os.environ.get("MONITOR_DB_GROWTH_RATIO", "0.25"))
# Тревога, если новых посетителей меньше этой доли от медианы за неделю
TRAFFIC_DROP_RATIO = float(os.environ.get("MONITOR_TRAFFIC_DROP_RATIO", "0.3"))
# Проверка трафика включается только когда медиана выше этого порога —
# на маленьких числах «падение на 70%» означает «было 3, стало 1».
TRAFFIC_MIN_BASELINE = int(os.environ.get("MONITOR_TRAFFIC_MIN_BASELINE", "10"))


def today_utc() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def _bounds(day: _dt.date) -> tuple[_dt.datetime, _dt.datetime]:
    """Границы суток в UTC: [начало дня, начало следующего)."""
    start = _dt.datetime.combine(day, _dt.time.min, tzinfo=_dt.timezone.utc)
    return start, start + _dt.timedelta(days=1)


# ─────────────────────────────────────────────────────────────────
# Здоровье системы
# ─────────────────────────────────────────────────────────────────

def system_health(cur) -> dict:
    """Размер БД, свободное место и число соединений — на момент вызова."""
    out: dict = {
        "db_size_bytes": None,
        "disk_free_bytes": None,
        "disk_total_bytes": None,
        "db_connections": None,
    }
    try:
        cur.execute("SELECT pg_database_size(current_database())")
        out["db_size_bytes"] = cur.fetchone()[0]
    except Exception as e:
        log.warning("monitoring: не удалось получить размер БД: %s", e)

    try:
        cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
        out["db_connections"] = cur.fetchone()[0]
    except Exception as e:
        log.warning("monitoring: не удалось получить число соединений: %s", e)

    # Диск меряем там, где лежит приложение: БД может быть на другой машине,
    # и тогда её том отсюда всё равно не виден.
    try:
        usage = shutil.disk_usage(os.path.dirname(os.path.abspath(__file__)))
        out["disk_free_bytes"] = usage.free
        out["disk_total_bytes"] = usage.total
    except Exception as e:
        log.warning("monitoring: не удалось получить данные о диске: %s", e)

    return out


def table_sizes(cur, limit: int = 12) -> list[dict]:
    """Самые тяжёлые таблицы — чтобы видеть, чем именно растёт БД."""
    cur.execute("""
        SELECT c.relname AS table_name,
               pg_total_relation_size(c.oid) AS bytes
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY bytes DESC
        LIMIT %s
    """, (limit,))
    return [{"table": r[0], "bytes": r[1]} for r in cur.fetchall()]


# ─────────────────────────────────────────────────────────────────
# Сбор суточного среза
# ─────────────────────────────────────────────────────────────────

def _scalar(cur, sql: str, params: tuple = ()) -> int:
    cur.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else 0


def _collect_counts(cur, day: _dt.date) -> dict:
    """Метрики, которые считаются одинаково и вживую, и задним числом.

    Накопительные итоги берутся «на конец суток». После cleanup-guests
    восстановленные итоги занижены: удалённых гостей уже не посчитать.
    """
    start, end = _bounds(day)

    return {
        # Состояние на конец суток
        "users_total": _scalar(cur,
            "SELECT count(*) FROM users WHERE created_at < %s", (end,)),
        "users_registered": _scalar(cur,
            "SELECT count(*) FROM users WHERE created_at < %s AND NOT is_guest", (end,)),
        "users_guests": _scalar(cur,
            "SELECT count(*) FROM users WHERE created_at < %s AND is_guest", (end,)),

        # Прирост за сутки
        "new_guests": _scalar(cur,
            "SELECT count(*) FROM users WHERE is_guest AND created_at >= %s AND created_at < %s",
            (start, end)),
        "new_registered": _scalar(cur,
            "SELECT count(*) FROM users WHERE NOT is_guest AND created_at >= %s AND created_at < %s",
            (start, end)),

        "cx_total": _scalar(cur,
            "SELECT count(*) FROM complexes WHERE created_at < %s", (end,)),
        "cx_public": _scalar(cur,
            "SELECT count(*) FROM complexes WHERE created_at < %s AND visibility = 'public'", (end,)),
        "cx_new": _scalar(cur,
            "SELECT count(*) FROM complexes WHERE created_at >= %s AND created_at < %s",
            (start, end)),
        "cx_new_public": _scalar(cur,
            "SELECT count(*) FROM complexes WHERE visibility = 'public' "
            "AND created_at >= %s AND created_at < %s", (start, end)),
        "cx_new_forks": _scalar(cur,
            "SELECT count(*) FROM complexes WHERE forked_from_id IS NOT NULL "
            "AND created_at >= %s AND created_at < %s", (start, end)),
        # Правки: комплекс изменён в эти сутки, но создан раньше
        "cx_edited": _scalar(cur,
            "SELECT count(*) FROM complexes WHERE updated_at >= %s AND updated_at < %s "
            "AND created_at < %s", (start, end, start)),

        "likes_new": _scalar(cur,
            "SELECT count(*) FROM complex_likes WHERE created_at >= %s AND created_at < %s",
            (start, end)),
        "subs_new": _scalar(cur,
            "SELECT count(*) FROM complex_subscriptions WHERE created_at >= %s AND created_at < %s",
            (start, end)),
    }


def _collect_breakdown(cur, day: _dt.date) -> dict:
    """Разрезы: откуда пришли, чем вошли, что популярно."""
    start, end = _bounds(day)

    cur.execute("""
        SELECT provider::text, count(*)
        FROM users WHERE NOT is_guest AND created_at < %s
        GROUP BY provider ORDER BY count(*) DESC
    """, (end,))
    providers = {r[0]: r[1] for r in cur.fetchall()}

    cur.execute("""
        SELECT COALESCE(NULLIF(utm_source, ''), '(прямой)'), count(*)
        FROM users WHERE created_at >= %s AND created_at < %s
        GROUP BY 1 ORDER BY count(*) DESC LIMIT 10
    """, (start, end))
    utm = {r[0]: r[1] for r in cur.fetchall()}

    # Реферер режем до хоста: полные URL засоряют и ничего не добавляют
    cur.execute("""
        SELECT COALESCE(NULLIF(split_part(split_part(referrer, '://', 2), '/', 1), ''),
                        '(прямой)'), count(*)
        FROM users WHERE created_at >= %s AND created_at < %s
        GROUP BY 1 ORDER BY count(*) DESC LIMIT 10
    """, (start, end))
    referrers = {r[0]: r[1] for r in cur.fetchall()}

    cur.execute("""
        SELECT c.name, c.likes_count
        FROM complexes c
        WHERE c.visibility = 'public' AND c.created_at < %s
        ORDER BY c.likes_count DESC, c.created_at DESC
        LIMIT 5
    """, (end,))
    top = [{"name": r[0], "likes": r[1]} for r in cur.fetchall()]

    return {"providers": providers, "utm": utm, "referrers": referrers, "top_complexes": top}


_UPSERT_COLUMNS = [
    "users_total", "users_registered", "users_guests",
    "new_guests", "new_registered", "active_24h", "active_7d",
    "cx_total", "cx_public", "cx_new", "cx_new_public", "cx_new_forks",
    "cx_edited", "likes_new", "subs_new",
    "db_size_bytes", "disk_free_bytes", "disk_total_bytes",
    "db_connections", "collect_ms",
]


def _upsert(cur, day: _dt.date, row: dict, breakdown: dict, backfilled: bool) -> None:
    """Пишет срез. Повторный сбор за те же сутки перезаписывает строку.

    У восстановленных строк COALESCE сохраняет уже измеренные значения:
    backfill не должен затирать живой сбор пустыми полями здоровья.
    """
    cols = ", ".join(_UPSERT_COLUMNS)
    placeholders = ", ".join(["%s"] * len(_UPSERT_COLUMNS))
    if backfilled:
        updates = ", ".join(
            f"{c} = COALESCE(EXCLUDED.{c}, daily_stats.{c})" for c in _UPSERT_COLUMNS)
    else:
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _UPSERT_COLUMNS)

    cur.execute(f"""
        INSERT INTO daily_stats (day, collected_at, is_backfilled, breakdown, {cols})
        VALUES (%s, NOW(), %s, %s::jsonb, {placeholders})
        ON CONFLICT (day) DO UPDATE SET
            collected_at  = NOW(),
            is_backfilled = EXCLUDED.is_backfilled AND daily_stats.is_backfilled,
            breakdown     = CASE WHEN EXCLUDED.breakdown = '{{}}'::jsonb
                                 THEN daily_stats.breakdown ELSE EXCLUDED.breakdown END,
            {updates}
    """, [day, backfilled, json.dumps(breakdown, ensure_ascii=False)]
         + [row.get(c) for c in _UPSERT_COLUMNS])


def collect_day(day: _dt.date | None = None, dry_run: bool = False) -> dict:
    """Собирает полный срез за сутки, включая здоровье и активность."""
    day = day or (today_utc() - _dt.timedelta(days=1))
    started = time.monotonic()

    with get_db() as con:
        with con.cursor() as cur:
            row = _collect_counts(cur, day)

            # Активность есть только для «сейчас»: last_seen_at не хранит историю.
            # Для прошлых суток оставляем пусто, чтобы не выдумывать число.
            if day == today_utc() or day == today_utc() - _dt.timedelta(days=1):
                row["active_24h"] = _scalar(cur,
                    "SELECT count(*) FROM users WHERE last_seen_at > NOW() - INTERVAL '24 hours'")
                row["active_7d"] = _scalar(cur,
                    "SELECT count(*) FROM users WHERE last_seen_at > NOW() - INTERVAL '7 days'")
            else:
                row["active_24h"] = None
                row["active_7d"] = None

            row.update(system_health(cur))
            breakdown = _collect_breakdown(cur, day)
            breakdown["tables"] = table_sizes(cur)
            row["collect_ms"] = int((time.monotonic() - started) * 1000)

            if not dry_run:
                _upsert(cur, day, row, breakdown, backfilled=False)
                con.commit()

    result = dict(row)
    result["day"] = day
    result["breakdown"] = breakdown
    return result


def backfill(days: int) -> int:
    """Восстанавливает прошлые сутки из created_at. Возвращает число строк.

    Заполняет только то, что выводится из таймстампов. Поля здоровья и
    активности остаются пустыми — на дашборде это видно как пропуск, и это
    честнее, чем подставить туда сегодняшние значения.
    """
    end_day = today_utc() - _dt.timedelta(days=1)
    written = 0

    with get_db() as con:
        with con.cursor() as cur:
            for offset in range(days, 0, -1):
                day = end_day - _dt.timedelta(days=offset - 1)
                row = _collect_counts(cur, day)
                # Нет ни пользователей, ни комплексов на эту дату — сайта ещё не было
                if row["users_total"] == 0 and row["cx_total"] == 0:
                    continue
                for field in ("active_24h", "active_7d", "db_size_bytes",
                              "disk_free_bytes", "disk_total_bytes",
                              "db_connections", "collect_ms"):
                    row[field] = None
                _upsert(cur, day, row, _collect_breakdown(cur, day), backfilled=True)
                written += 1
            con.commit()
    return written


# ─────────────────────────────────────────────────────────────────
# Проверки
# ─────────────────────────────────────────────────────────────────

def _median(values: list) -> float:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2


def evaluate_checks(current: dict, history: list[dict]) -> list[dict]:
    """Сверяет срез с историей. Возвращает [{kind, severity, message}].

    Чистая функция без обращений к БД — её удобно проверять тестами.
    `history` — предыдущие сутки, новее первыми, текущие в неё не входят.
    """
    alerts: list[dict] = []

    # ── Диск ────────────────────────────────────────────────────
    free = current.get("disk_free_bytes")
    total = current.get("disk_total_bytes")
    if free and total:
        ratio = free / total
        if ratio < DISK_FREE_MIN_RATIO:
            alerts.append({
                "kind": "disk_low",
                "severity": "critical" if ratio < 0.05 else "warning",
                "message": (f"На диске свободно {ratio * 100:.1f}% "
                            f"({free / 2**30:.1f} ГБ из {total / 2**30:.1f} ГБ). "
                            f"Порог — {DISK_FREE_MIN_RATIO * 100:.0f}%."),
            })

    # ── Рост БД ─────────────────────────────────────────────────
    size = current.get("db_size_bytes")
    prev_size = next((h["db_size_bytes"] for h in history if h.get("db_size_bytes")), None)
    if size and prev_size:
        growth = (size - prev_size) / prev_size
        if growth > DB_GROWTH_MAX_RATIO:
            alerts.append({
                "kind": "db_growth",
                "severity": "warning",
                "message": (f"База выросла на {growth * 100:.0f}% за сутки: "
                            f"{prev_size / 2**20:.0f} МБ → {size / 2**20:.0f} МБ."),
            })

    # ── Трафик ──────────────────────────────────────────────────
    new_guests = current.get("new_guests")
    baseline = _median([h.get("new_guests") for h in history[:7]])

    if new_guests == 0 and baseline >= 1:
        alerts.append({
            "kind": "no_traffic",
            "severity": "critical",
            "message": ("За сутки ни одного нового посетителя, хотя обычно "
                        f"около {baseline:.0f}. Похоже, сайт недоступен."),
        })
    elif (new_guests is not None and baseline >= TRAFFIC_MIN_BASELINE
          and new_guests < baseline * TRAFFIC_DROP_RATIO):
        alerts.append({
            "kind": "traffic_drop",
            "severity": "warning",
            "message": (f"Новых посетителей за сутки: {new_guests}, "
                        f"медиана за неделю — {baseline:.0f}."),
        })

    # ── Пропуск сбора ───────────────────────────────────────────
    # Ловится только когда сбор всё-таки запустился: если процесс не стартовал,
    # сообщить об этом изнутри некому. Дашборд отдельно показывает свежесть.
    if history:
        gap = (current["day"] - history[0]["day"]).days
        if gap > 1:
            alerts.append({
                "kind": "collector_gap",
                "severity": "warning",
                "message": (f"В данных пропуск: предыдущий срез за {history[0]['day']}, "
                            f"то есть {gap - 1} суток не собирались."),
            })

    return alerts


def run_checks(day: _dt.date, send_email=None) -> list[dict]:
    """Прогоняет проверки за сутки и записывает новые срабатывания.

    Письмо уходит только по тем проверкам, которых за эти сутки ещё не было:
    UNIQUE(day, kind) не даст записать повтор, и по числу вставленных строк
    видно, что именно ново.
    """
    with get_db() as con:
        with dict_cursor(con) as cur:
            cur.execute("SELECT * FROM daily_stats WHERE day = %s", (day,))
            current = cur.fetchone()
            if not current:
                return []
            cur.execute("""
                SELECT * FROM daily_stats WHERE day < %s ORDER BY day DESC LIMIT 14
            """, (day,))
            history = [dict(r) for r in cur.fetchall()]

            alerts = evaluate_checks(dict(current), history)
            fresh = []
            for a in alerts:
                cur.execute("""
                    INSERT INTO monitor_alerts (day, kind, severity, message)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (day, kind) DO NOTHING
                    RETURNING id
                """, (day, a["kind"], a["severity"], a["message"]))
                if cur.fetchone():
                    fresh.append(a)
            con.commit()

    if fresh and send_email:
        _notify(day, fresh, send_email)
    return fresh


def _notify(day: _dt.date, alerts: list[dict], send_email) -> None:
    """Одно письмо на весь набор новых тревог за сутки."""
    worst = "critical" if any(a["severity"] == "critical" for a in alerts) else "warning"
    mark = "!!" if worst == "critical" else "!"
    subject = f"[CoI Planner] {mark} мониторинг за {day}: {len(alerts)}"

    lines = [f"Проверки за {day}:", ""]
    for a in alerts:
        lines.append(f"  [{a['severity']}] {a['message']}")
    lines += ["", "Подробности: /admin/stats"]
    body = "\n".join(lines)

    try:
        send_email(subject, body)
    except Exception as e:
        log.error("monitoring: не удалось отправить письмо: %s", e)
        return

    with get_db() as con:
        with con.cursor() as cur:
            cur.execute("""
                UPDATE monitor_alerts SET notified_at = NOW()
                WHERE day = %s AND kind = ANY(%s) AND notified_at IS NULL
            """, (day, [a["kind"] for a in alerts]))
            con.commit()


# ─────────────────────────────────────────────────────────────────
# Данные для дашборда
# ─────────────────────────────────────────────────────────────────

def dashboard_data(days: int = DEFAULT_WINDOW_DAYS) -> dict:
    """Всё, что рисует /admin/stats: ряды, текущее состояние, тревоги."""
    with get_db() as con:
        with dict_cursor(con) as cur:
            cur.execute("""
                SELECT * FROM daily_stats
                WHERE day > CURRENT_DATE - %s::int
                ORDER BY day
            """, (days + 1,))
            series = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT * FROM monitor_alerts
                ORDER BY created_at DESC LIMIT 20
            """)
            alerts = [dict(r) for r in cur.fetchall()]

        # «Сейчас» считаем вживую: последний срез может быть вчерашним.
        # Отдельный обычный курсор: _scalar и system_health читают строки
        # по индексу, а dict_cursor отдаёт словари.
        with con.cursor() as cur:
            live = {
                "users_total": _scalar(cur, "SELECT count(*) FROM users"),
                "users_registered": _scalar(cur, "SELECT count(*) FROM users WHERE NOT is_guest"),
                "cx_total": _scalar(cur, "SELECT count(*) FROM complexes"),
                "cx_public": _scalar(cur,
                    "SELECT count(*) FROM complexes WHERE visibility = 'public'"),
                "active_24h": _scalar(cur,
                    "SELECT count(*) FROM users WHERE last_seen_at > NOW() - INTERVAL '24 hours'"),
                "new_guests_24h": _scalar(cur,
                    "SELECT count(*) FROM users WHERE is_guest "
                    "AND created_at > NOW() - INTERVAL '24 hours'"),
                "new_registered_7d": _scalar(cur,
                    "SELECT count(*) FROM users WHERE NOT is_guest "
                    "AND created_at > NOW() - INTERVAL '7 days'"),
                "cx_new_7d": _scalar(cur,
                    "SELECT count(*) FROM complexes WHERE created_at > NOW() - INTERVAL '7 days'"),
            }
            live.update(system_health(cur))
            live["tables"] = table_sizes(cur, limit=8)

    last = series[-1] if series else None
    stale_hours = None
    if last and last.get("collected_at"):
        delta = _dt.datetime.now(_dt.timezone.utc) - last["collected_at"]
        stale_hours = round(delta.total_seconds() / 3600, 1)

    return {
        "series": series,
        "alerts": alerts,
        "live": live,
        "last_day": last["day"] if last else None,
        "stale_hours": stale_hours,
        "window_days": days,
    }
