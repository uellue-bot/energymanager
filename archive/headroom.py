"""Solar headroom for the next 48 h, a first version (requirements S5; change solar-headroom).

The headroom of a one-hour window is the largest extra energy, spread evenly over the
hour, that leaves the simulated grid import over the horizon unchanged (E23, E24, E26).
Its inputs: the PV forecast (forecast.solar, Open-Meteo where forecast.solar has no value),
an expected household load (a daily load curve scaled by a daily fit against heating
degree days), and the battery, simulated for self-consumption from the latest state of
charge in the inverter's records. Standard library only.
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import forecast_archive as fa

STEP = timedelta(minutes=15)
STEPS = 192  # 48 h
WINDOW = 4  # steps in one hour
DT_H = 0.25

# The battery (requirements, PV inverter and battery; design decision 4).
BATTERY_KWH = 5.7  # nominal; three 1.9 kWh units
SOC_MIN, SOC_MAX = 0.07, 0.97  # the inverter's limits, as Home Assistant reports them
BATTERY_KW = 5.0  # the principal's figure
INVERTER_KW = 10.0  # AC limit of PV plus battery
# Energy discharged ÷ energy charged over the battery's life, from the inverter's lifetime
# counters as Home Assistant reported them on 2026-11-27 (2,584,884 / 2,823,064 Wh).
ROUND_TRIP = 0.916
ETA = ROUND_TRIP ** 0.5  # per direction

# Inputs older than this give no headroom (spec: an input is missing).
MAX_FORECAST_AGE = timedelta(hours=6)
MAX_RECORD_AGE = timedelta(hours=2)  # the inverter's records reach the collector about an hour late
LIVE_SOC_AGE = timedelta(minutes=5)  # the collector reads the live state of charge every minute

TOLERANCE_KWH = 0.001  # extra import still counted as none (rounding in the simulation)
CURVE_DAYS = 28
MIN_CURVE_DAYS = 2  # per day type
MIN_FIT_DAYS = 28
CORRECTION_MIN_KWH = 1.0
LOG_SERIES = {"load": "logger.minutes_eload_log_ts", "pv_a": "logger.minutes_ea_log_ts",
              "pv_b": "logger.minutes_eb_log_ts", "soc": "logger.minutes_soc_log_ts"}


class Missing(Exception):
    """An input the forecast needs is missing or stale."""


def floor_step(t: datetime) -> datetime:
    t = t.astimezone(timezone.utc)
    return t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)


# --- the inverter's 5-minute records -------------------------------------------------------

def clock_offset_s(collector_conn) -> int:
    """The inverter's clock minus UTC, in seconds, at the collector's latest query (table clock)."""
    row = collector_conn.execute(
        "SELECT inverter_ts - CAST(strftime('%s', read_at) AS INTEGER) FROM clock"
        " ORDER BY read_at DESC LIMIT 1").fetchone()
    if row is None:
        raise Missing("the collector has not recorded the inverter's clock")
    return row[0]


def records(collector_conn, series: str, since: datetime, offset_s: int) -> list[tuple[datetime, float]]:
    """(UTC end of the 5 minutes, value) of one 5-minute series from `since` on. The values of
    the energy series are mean power in W over the 5 minutes (value / 12 = Wh)."""
    rows = collector_conn.execute(
        "SELECT ts, value FROM log_entry WHERE series = ? AND ts > ? ORDER BY ts",
        (series, int(since.timestamp()) + offset_s))
    return [(datetime.fromtimestamp(ts - offset_s, timezone.utc), v) for ts, v in rows]


def latest_soc(collector_conn, offset_s: int, now: datetime) -> tuple[datetime, float]:
    """(UTC time, state of charge as a fraction): the live value the collector reads every
    minute if it is at most LIVE_SOC_AGE old, else the latest of the inverter's 5-minute
    records, which reach the store with the hourly catch-up."""
    try:
        live = collector_conn.execute(
            "SELECT read_at, value FROM state WHERE object = 'battery.soc' ORDER BY read_at DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:  # a store from before the live value was read
        live = None
    if live is not None:
        t = fa.parse_time(live[0])
        if timedelta(0) <= now - t <= LIVE_SOC_AGE:
            return t, live[1]
    row = collector_conn.execute("SELECT ts, value FROM log_entry WHERE series = ? ORDER BY ts DESC LIMIT 1",
                                 (LOG_SERIES["soc"],)).fetchone()
    if row is None:
        raise Missing("no state of charge recorded")
    t = datetime.fromtimestamp(row[0] - offset_s, timezone.utc)
    if now - t > MAX_RECORD_AGE:
        raise Missing(f"the latest state of charge is from {fa.iso(t)}")
    if t > now:  # e.g. the inverter's clock was set back and its older records now look ahead
        raise Missing(f"the latest state of charge is stamped in the future ({fa.iso(t)})")
    return t, row[1]


# --- expected household load ---------------------------------------------------------------

def day_type(d: date) -> str:
    return "weekend" if d.weekday() >= 5 else "weekday"


def load_curves(collector_conn, offset_s: int, now: datetime, tz: ZoneInfo) -> dict[str, list[float]]:
    """Per day type, the mean share of each 15-minute slot of the local day in the day's load,
    over the last CURVE_DAYS complete local days (design decision 3). Days without all 288
    records, or with a clock change, are left out."""
    today = now.astimezone(tz).date()
    first = today - timedelta(days=CURVE_DAYS)
    start = datetime(first.year, first.month, first.day, tzinfo=tz)
    per_day: dict[date, list[float]] = {}
    counts: dict[date, int] = {}
    for end, w in records(collector_conn, LOG_SERIES["load"], start, offset_s):
        local = (end - timedelta(minutes=5)).astimezone(tz)
        d = local.date()
        if d >= today:
            continue
        counts[d] = counts.get(d, 0) + 1
        slot = (local.hour * 60 + local.minute) // 15
        per_day.setdefault(d, [0.0] * 96)
        if slot < 96:
            per_day[d][slot] += w / 12
    shares: dict[str, list[list[float]]] = {"weekday": [], "weekend": []}
    for d, slots in per_day.items():
        total = sum(slots)
        nxt = d + timedelta(days=1)
        hours = (datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz) - datetime(d.year, d.month, d.day, tzinfo=tz))
        # A day of 24 h (not a clock change) with at most half an hour of records missing.
        if hours == timedelta(hours=24) and counts.get(d, 0) >= 282 and total > 0:
            shares[day_type(d)].append([s / total for s in slots])
    curves = {}
    for kind, days in shares.items():
        if len(days) < MIN_CURVE_DAYS:
            raise Missing(f"fewer than {MIN_CURVE_DAYS} complete {kind}s in the last {CURVE_DAYS} days")
        curves[kind] = [sum(day[i] for day in days) / len(days) for i in range(96)]
    return curves


def fit_daily_load(days: list[tuple[float, float]]) -> tuple[float, float, float]:
    """(constant kWh, kWh per degree day, base temperature) of daily load = constant + slope ×
    max(0, base − daily mean temperature), the base scanned as in the baseline model."""
    if len(days) < MIN_FIT_DAYS:
        raise Missing(f"fewer than {MIN_FIT_DAYS} days with load and temperature")
    best = None
    for base in fa.BASE_TEMPERATURES:
        rows = [[1.0, max(0.0, base - t)] for t, _ in days]
        try:
            c = fa.least_squares(rows, [kwh for _, kwh in days])
        except ZeroDivisionError:
            continue
        sse = sum((c[0] + c[1] * r[1] - kwh) ** 2 for r, (_, kwh) in zip(rows, days))
        if best is None or sse < best[0]:
            best = (sse, c[0], c[1], base)
    if best is None:
        raise Missing("the daily load cannot be fitted")
    return best[1], best[2], best[3]


def forecast_temperatures(conn, now: datetime) -> dict[datetime, float]:
    """Hourly temperature: the historical values, and the latest Open-Meteo forecast on top."""
    temps = fa.history_temperatures(conn)
    issue = fa.latest_before(conn, fa.OPEN_METEO, now)
    if issue is not None:
        hourly = fa.json.loads(issue["body"])["hourly"]
        for stamp, value in zip(hourly["time"], hourly["temperature_2m"]):
            if value is not None:
                temps[fa.parse_time(stamp)] = value
    return temps


def forecast_day_mean(temps: dict[datetime, float], day: date, tz: ZoneInfo) -> float | None:
    """Mean temperature of a local day from at least 20 of its hours. The history ends with
    yesterday (UTC) and may still lack the last hours, where the forecast, starting at today's
    UTC midnight, has not begun yet."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)
    nxt = day + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).astimezone(timezone.utc)
    values = [temps[h] for h in (start + timedelta(hours=k) for k in range(int((end - start) / timedelta(hours=1))))
              if h in temps]
    return sum(values) / len(values) if len(values) >= 20 else None


def expected_load(conn, collector_conn, offset_s: int, start: datetime, n: int, now: datetime,
                  tz: ZoneInfo) -> tuple[list[float], dict]:
    """Expected household load in kWh for n steps from `start`, and the fit's figures."""
    temps = forecast_temperatures(conn, now)
    history = []
    for d, e in fa.inverter_days(collector_conn, fa.HISTORY_START).items():
        t = fa.day_mean_temperature(temps, d, tz)
        if "load" in e and t is not None and d < now.astimezone(tz).date():
            history.append((t, e["load"]))
    constant, slope, base = fit_daily_load(history)
    curves = load_curves(collector_conn, offset_s, now, tz)
    loads = []
    for k in range(n):
        local = (start + k * STEP).astimezone(tz)
        t = forecast_day_mean(temps, local.date(), tz)
        if t is None:
            raise Missing(f"no temperature forecast for {local.date()}")
        day_kwh = constant + slope * max(0.0, base - t)
        loads.append(max(0.0, day_kwh) * curves[day_type(local.date())][(local.hour * 60 + local.minute) // 15])
    return loads, {"base_temperature_c": base, "constant_kwh": round(constant, 2),
                   "kwh_per_degree_day": round(slope, 3), "fit_days": len(history)}


# --- expected PV ---------------------------------------------------------------------------

def hourly_pv(conn, now: datetime, kwp: float) -> dict[datetime, float]:
    """Wh per UTC hour from the latest forecast.solar issue; hours it lacks from the latest
    Open-Meteo estimate, scaled by forecast.solar ÷ Open-Meteo over the hours both have, since
    the Open-Meteo estimate has no losses (design decision 2)."""
    fs = fa.latest_before(conn, fa.FORECAST_SOLAR, now)
    om = fa.latest_before(conn, fa.OPEN_METEO, now)
    fresh = [i for i in (fs, om) if i is not None and now - fa.parse_time(i["fetched_at"]) <= MAX_FORECAST_AGE]
    if not fresh:
        raise Missing("no PV forecast from the last 6 hours")
    if fs is None:
        raise Missing("no forecast.solar issue to scale Open-Meteo's estimate to")
    meteo = fa.open_meteo_hourly_wh(om["body"], kwp) if om in fresh else {}
    # The scale comes from the latest forecast.solar issue even when it is older than 6 h,
    # compared with the Open-Meteo issue fetched with it; only fresh values are used.
    scaling_solar = fa.forecast_solar_hourly_wh(fs["body"])
    paired = fa.latest_before(conn, fa.OPEN_METEO, fa.parse_time(fs["fetched_at"])) or om
    scaling_meteo = fa.open_meteo_hourly_wh(paired["body"], kwp) if paired else {}
    both = [h for h in scaling_solar if scaling_meteo.get(h, 0) > 0]
    if meteo and not both:
        raise Missing("forecast.solar and Open-Meteo share no hour to scale by")
    scale = sum(scaling_solar[h] for h in both) / sum(scaling_meteo[h] for h in both) if both else 1.0
    solar = scaling_solar if fs in fresh else {}
    return {**{h: v * scale for h, v in meteo.items()}, **solar}


def pv_steps(hourly: dict[datetime, float], start: datetime, n: int) -> list[float]:
    """kWh per 15-minute step; each hour's energy spread evenly over its four steps. An hour
    the forecast does not list before its last listed hour counts as 0 (forecast.solar omits
    the night); an hour after it is missing."""
    last = max(hourly, default=None)
    out = []
    for k in range(n):
        hour = (start + k * STEP).replace(minute=0)
        if hour in hourly:
            out.append(hourly[hour] / 4000)
        elif last is not None and hour < last:
            out.append(0.0)
        else:
            raise Missing(f"no PV forecast for {fa.iso(hour)}")
    return out


def correction_factor(collector_conn, offset_s: int, hourly: dict[datetime, float], now: datetime,
                      tz: ZoneInfo) -> float | None:
    """Actual ÷ forecast PV of today so far, over the whole hours recorded (design decision 8);
    None below CORRECTION_MIN_KWH of actual production."""
    local = now.astimezone(tz)
    midnight = datetime(local.year, local.month, local.day, tzinfo=tz).astimezone(timezone.utc)
    actual: dict[datetime, float] = {}
    counts: dict[datetime, int] = {}
    for key in ("pv_a", "pv_b"):
        for end, w in records(collector_conn, LOG_SERIES[key], midnight, offset_s):
            hour = (end - timedelta(minutes=5)).replace(minute=0, second=0, microsecond=0)
            actual[hour] = actual.get(hour, 0.0) + w / 12
            if key == "pv_a":
                counts[hour] = counts.get(hour, 0) + 1
    hours = [h for h in actual if counts.get(h) == 12 and h in hourly]
    produced = sum(actual[h] for h in hours) / 1000
    forecast = sum(hourly[h] for h in hours) / 1000
    if produced < CORRECTION_MIN_KWH or forecast <= 0:
        return None
    return produced / forecast


# --- battery and headroom ------------------------------------------------------------------

def simulate(pv: list[float], load: list[float], soc: float) -> tuple[float, list[float]]:
    """Self-consumption in 15-minute steps (design decision 4): (grid import in kWh over all
    steps, state of charge after each step)."""
    e = soc * BATTERY_KWH
    lo, hi = SOC_MIN * BATTERY_KWH, SOC_MAX * BATTERY_KWH
    cap = INVERTER_KW * DT_H
    grid = 0.0
    socs = []
    for p, l in zip(pv, load):
        net = p - l
        if net >= 0:
            charge = min(net, BATTERY_KW * DT_H, max(0.0, hi - e) / ETA)
            e += charge * ETA
        else:
            supplied = min(p, l)  # PV used in the house goes through the inverter as well
            discharge = min(-net, BATTERY_KW * DT_H, max(0.0, e - lo) * ETA, max(0.0, cap - supplied))
            e -= discharge / ETA
            grid += -net - discharge
        socs.append(e / BATTERY_KWH)
    return grid, socs


def headroom(pv: list[float], load: list[float], soc: float, windows: int, first: int = 0) -> list[float]:
    """kWh per window of WINDOW steps, for `windows` windows from step `first` (design
    decision 5): the largest extra energy spread over the window that raises the simulated
    import by at most 1 Wh, found to 10 Wh and rounded down. Energy the battery holds less
    at the end of the horizon counts as import too, since the night after it needs it. pv
    and load must cover first + windows + WINDOW − 1 steps."""
    base, base_socs = simulate(pv, load, soc)
    out = []
    for i in range(first, first + windows):
        def extra_import(x):
            more = list(load)
            for k in range(i, i + WINDOW):
                more[k] += x / WINDOW
            grid, socs = simulate(pv, more, soc)
            return grid - base + max(0.0, base_socs[-1] - socs[-1]) * BATTERY_KWH * ETA
        lo, hi = 0.0, INVERTER_KW
        if extra_import(0.01) > TOLERANCE_KWH:
            out.append(0.0)
            continue
        lo = 0.01
        while hi - lo > 0.01:
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if extra_import(mid) <= TOLERANCE_KWH else (lo, mid)
        out.append(int(lo * 100) / 100)
    return out


def forecast(conn, collector_conn, now: datetime, kwp: float, tz: ZoneInfo) -> dict:
    """The headroom forecast for the 48 h from the current 15-minute step, with and without the
    correction from today's production. Raises Missing when an input is missing or stale."""
    offset = clock_offset_s(collector_conn)
    soc_time, soc = latest_soc(collector_conn, offset, now)
    first = floor_step(now)
    start = floor_step(soc_time)  # simulate from the latest record, over the gap until now
    skip = int((first - start) / STEP)
    n = skip + STEPS + WINDOW - 1
    hourly = hourly_pv(conn, now, kwp)
    pv = pv_steps(hourly, start, n)
    load, fit = expected_load(conn, collector_conn, offset, start, n, now, tz)
    factor = correction_factor(collector_conn, offset, hourly, now, tz)
    today = now.astimezone(tz).date()
    pv_corr = [p * factor if factor is not None and (start + k * STEP).astimezone(tz).date() == today else p
               for k, p in enumerate(pv)]
    room = headroom(pv, load, soc, STEPS, first=skip)
    room_corr = headroom(pv_corr, load, soc, STEPS, first=skip) if factor is not None else room
    _, socs = simulate(pv, load, soc)
    steps = [{"start": fa.iso(first + k * STEP), "pv_kwh": round(pv[skip + k], 3),
              "pv_corrected_kwh": round(pv_corr[skip + k], 3), "load_kwh": round(load[skip + k], 3),
              "soc": round(socs[skip + k], 3), "headroom_kwh": room[k],
              "headroom_corrected_kwh": room_corr[k]} for k in range(STEPS)]
    return {"made_at": fa.iso(now), "soc_recorded_at": fa.iso(soc_time), "soc_recorded": round(soc, 3),
            "correction_factor": round(factor, 3) if factor is not None else None,
            "load_fit": fit, "steps": steps}


# --- the record of predictions (E25) -------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS headroom_forecast (
    made_at TEXT NOT NULL,          -- UTC, ISO 8601, when the forecast was made
    step_start TEXT NOT NULL,       -- UTC, ISO 8601, start of the 15-minute step
    pv_kwh REAL NOT NULL,
    pv_corrected_kwh REAL NOT NULL, -- with the correction from today's production (design 8)
    load_kwh REAL NOT NULL,
    soc REAL NOT NULL,              -- simulated state of charge at the step's end, a fraction
    headroom_kwh REAL NOT NULL,     -- of the hour starting at step_start
    headroom_corrected_kwh REAL NOT NULL,
    UNIQUE (made_at, step_start)
);
CREATE INDEX IF NOT EXISTS headroom_forecast_step ON headroom_forecast (step_start);
"""


def recorded_this_hour(conn, now: datetime) -> bool:
    start = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return conn.execute("SELECT 1 FROM headroom_forecast WHERE made_at >= ? AND made_at < ? LIMIT 1",
                        (fa.iso(start), fa.iso(start + timedelta(hours=1)))).fetchone() is not None


def record(conn, result: dict) -> int:
    """Store a forecast's steps unless one was stored this hour already; returns the rows written."""
    conn.executescript(SCHEMA)
    if recorded_this_hour(conn, fa.parse_time(result["made_at"])):
        return 0
    rows = [(result["made_at"], s["start"], s["pv_kwh"], s["pv_corrected_kwh"], s["load_kwh"], s["soc"],
             s["headroom_kwh"], s["headroom_corrected_kwh"]) for s in result["steps"]]
    with conn:
        conn.executemany("INSERT OR IGNORE INTO headroom_forecast VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)
