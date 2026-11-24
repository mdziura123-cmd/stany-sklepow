"""Diagnostyka pobrań: czasy, skuteczność, punktualność harmonogramu."""
from __future__ import annotations

import math
import os
import statistics
from datetime import datetime, timedelta

from . import config, db, presets, runner
from .scraper import USER_AGENT

GOOD = ("ok", "częściowy")
STATUSES = ("ok", "częściowy", "błąd", "przerwany", "trwa")
STATUS_VAR = {"ok": "--good", "częściowy": "--warn", "błąd": "--bad", "przerwany": "--bad", "trwa": "--series-1"}
PERIOD_DAYS = {"7": 7, "30": 30, "90": 90, "all": None}


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace(" ", "T"))
    except ValueError:
        return None
    # w bazie część czasów ma strefę (+01:00), a godziny harmonogramu nie – porównujemy czas lokalny
    return dt.astimezone(config.TZ).replace(tzinfo=None) if dt.tzinfo else dt


def _int(v, default=None):
    if v is None or v == "":
        return default
    try:
        return int(float(str(v).replace(",", ".")))
    except ValueError:
        return default


def fmt_dur(sec) -> str:
    if sec is None:
        return "–"
    sec = int(round(sec))
    if sec < 0:
        return "–"
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def fmt_delay(sec) -> str:
    if sec is None:
        return "–"
    sign = "−" if sec < 0 else "+"
    return sign + fmt_dur(abs(sec))


def _median(xs):
    return statistics.median(xs) if xs else None


def _avg(xs):
    return sum(xs) / len(xs) if xs else None


def _iavg(xs):
    a = _avg(xs)
    return int(round(a)) if a is not None else None


def _p90(xs):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, math.ceil(0.9 * len(s)) - 1)]


def enrich(r) -> dict:
    """Jeden wiersz z tabeli runs + wyliczone czasy."""
    d = dict(r)
    for k in ("found", "requests", "errors", "inc_requests", "inc_checked", "inc_errors"):
        d[k] = _int(d.get(k), None if k != "found" and k != "requests" else 0)
    st, fin = _dt(d["started"]), _dt(d["finished"])
    slot = _dt(d.get("ts")) if d.get("ts") and len(d["ts"]) == 16 else None
    inc_st, inc_fin = _dt(d.get("inc_started")), _dt(d.get("inc_finished"))
    d["start_dt"], d["end_dt"] = st, fin
    d["dur"] = (fin - st).total_seconds() if st and fin and d["status"] != "trwa" else None
    if d["status"] == "trwa" and st:
        d["dur"] = (config.now().replace(tzinfo=None) - st).total_seconds()
    d["inc_dur"] = (inc_fin - inc_st).total_seconds() if inc_st and inc_fin else None
    d["total_dur"] = ((inc_fin or fin) - st).total_seconds() if st and (inc_fin or fin) else d["dur"]
    d["all_end"] = inc_fin or fin
    d["planned"] = slot is not None and slot.minute == 0
    d["slot_dt"] = slot if d["planned"] else None
    d["slot_hour"] = slot.hour if d["planned"] else None
    d["delay"] = (st - slot).total_seconds() if d["planned"] and st else None
    d["rpm"] = round(d["requests"] / (d["dur"] / 60)) if d["dur"] and d["dur"] > 0 and d["requests"] else None
    d["err_pct"] = (100 * d["errors"] / d["requests"]) if d.get("errors") is not None and d["requests"] else None
    d["good"] = d["status"] in GOOD
    return d


def _since(period: str) -> str | None:
    days = PERIOD_DAYS.get(period)
    if not days:
        return None
    return (config.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def coverage(shop_id: int, runs: list[dict], since: str | None, hours: list[int] | None = None) -> dict:
    """Ile zaplanowanych odczytów (w obecnym harmonogramie) się udało, a których brakuje."""
    hours = hours if hours is not None else db.run_hours()
    now = config.now().replace(tzinfo=None)
    planned = [r for r in runs if r["planned"] and r["slot_hour"] in hours]
    if not hours or not planned:
        return {"expected": 0, "done": 0, "pct": None, "missing": [], "from": None}
    first = min(r["slot_dt"] for r in planned).date()
    if since:
        first = max(first, datetime.fromisoformat(since).date())
    good_slots = {r["slot_dt"] for r in planned if r["good"]}
    tried = {r["slot_dt"] for r in planned}
    expected, missing = 0, []
    day = first
    while day <= now.date():
        for h in hours:
            slot = datetime(day.year, day.month, day.day, h)
            # bieżący slot liczymy dopiero po 2 godzinach – pobieranie może jeszcze trwać
            if slot > now - timedelta(hours=2):
                continue
            expected += 1
            if slot not in good_slots:
                missing.append({"slot": slot, "tried": slot in tried})
        day += timedelta(days=1)
    done = expected - len(missing)
    return {"expected": expected, "done": done, "pct": 100 * done / expected if expected else None,
            "missing": list(reversed(missing))[:15], "missing_n": len(missing), "from": first}


def by_slot(runs: list[dict], hours: list[int] | None = None) -> list[dict]:
    hours = hours if hours is not None else db.run_hours()
    keys = sorted({r["slot_hour"] for r in runs if r["planned"]} | set(hours))
    out = []
    for h in keys + ["ręczne"]:
        rs = [r for r in runs if (r["slot_hour"] == h if h != "ręczne" else not r["planned"])
              and r["status"] != "trwa"]
        if not rs and (h == "ręczne" or h not in hours):
            continue
        good = [r for r in rs if r["good"]]
        durs = [r["dur"] for r in good if r["dur"] is not None]
        delays = [r["delay"] for r in rs if r["delay"] is not None]
        out.append({
            "label": f"{h}:00" if h != "ręczne" else "ręczne",
            "current": h in hours,
            "n": len(rs), "good": len(good),
            "pct": 100 * len(good) / len(rs) if rs else None,
            "delay_avg": _avg(delays), "delay_max": max(delays) if delays else None,
            "dur_med": _median(durs), "dur_max": max(durs) if durs else None,
            "found_avg": _iavg([r["found"] for r in good]),
            "req_avg": _iavg([r["requests"] for r in good]),
            "last": rs[0] if rs else None,
        })
    return out


def chart_items(runs: list[dict], limit: int = 40) -> list[dict]:
    items = []
    for r in reversed([r for r in runs if r["dur"] is not None][:limit]):
        st = r["start_dt"]
        tip = (f"{st:%d.%m %H:%M:%S}|status: {r['status']}|stany: {fmt_dur(r['dur'])}"
               + (f"|dostawy: {fmt_dur(r['inc_dur'])}" if r["inc_dur"] else "")
               + f"|produktów: {r['found']}|zapytań: {r['requests']}"
               + (f"|opóźnienie: {fmt_delay(r['delay'])}" if r["delay"] is not None else "|ręczne"))
        items.append({"label": f"{st:%d.%m %H}", "value": round(r["dur"] / 60, 1), "tip": tip,
                      "var": STATUS_VAR.get(r["status"], "--series-1")})
    return items


def system_info(shop: dict) -> dict:
    cfg = presets.merged(shop["config"])
    now = config.now().replace(tzinfo=None)
    hours = db.shop_run_hours(shop)
    nxt = None
    for add in range(0, 2):
        day = now.date() + timedelta(days=add)
        for h in hours:
            cand = datetime(day.year, day.month, day.day, h)
            if cand > now:
                nxt = cand
                break
        if nxt:
            break
    try:
        db_size = os.path.getsize(db.db_path())
    except OSError:
        db_size = None
    return {
        "now": now, "next": nxt, "hours": hours,
        "running": dict(runner.status) if runner.status.get("running") else None,
        "db_size": db_size, "version": config.VERSION, "sha": config.GIT_SHA,
        "delay": cfg.get("delay_seconds"), "max_pages": cfg.get("max_pages"),
        "incoming_pages": cfg.get("incoming_pages"), "robots": cfg.get("respect_robots", True),
        "mode": (cfg.get("detected") or {}).get("mode"), "user_agent": USER_AGENT,
        "preset": cfg.get("preset") or "własna konfiguracja", "preset_version": cfg.get("preset_version"),
    }


def report(shop: dict, period: str = "30") -> dict:
    since = _since(period)
    runs = [enrich(r) for r in db.runs_since(shop["id"], since)]
    done = [r for r in runs if r["status"] != "trwa"]
    good = [r for r in done if r["good"]]
    durs = [r["dur"] for r in good if r["dur"] is not None]
    inc = [r["inc_dur"] for r in done if r["inc_dur"] is not None]
    delays = [r["delay"] for r in done if r["delay"] is not None]
    counts = {s: sum(1 for r in runs if r["status"] == s) for s in STATUSES}
    last_good = next((r for r in runs if r["good"]), None)
    now = config.now().replace(tzinfo=None)
    return {
        "period": period, "runs": runs, "log": runs[:200],
        "n": len(done), "counts": counts,
        "success_pct": 100 * len(good) / len(done) if done else None,
        "full_pct": 100 * counts["ok"] / len(done) if done else None,
        "dur_med": _median(durs), "dur_avg": _avg(durs), "dur_p90": _p90(durs),
        "dur_min": min(durs) if durs else None, "dur_max": max(durs) if durs else None,
        "inc_med": _median(inc),
        "delay_avg": _avg(delays), "delay_max": max(delays) if delays else None,
        "req_avg": _iavg([r["requests"] for r in good]), "req_total": sum(r["requests"] for r in done),
        "rpm_avg": _iavg([r["rpm"] for r in good if r["rpm"]]),
        "found_avg": _iavg([r["found"] for r in good]),
        "err_total": sum(r["errors"] or 0 for r in done),
        "last_good": last_good,
        "since_good": (now - last_good["all_end"]).total_seconds() if last_good and last_good["all_end"] else None,
        "coverage": coverage(shop["id"], runs, since, db.shop_run_hours(shop)),
        "slots": by_slot(runs, db.shop_run_hours(shop)),
        "chart": chart_items(runs),
        "sys": system_info(shop),
    }
