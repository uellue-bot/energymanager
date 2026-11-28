"""Tests for archive/headroom.py (spec: solar-headroom; examples E23 to E26). Synthetic data only."""

import json
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "archive"))
import forecast_archive as fa  # noqa: E402
import headroom as hr  # noqa: E402

BERLIN = fa.ZoneInfo("Europe/Berlin")
OFFSET = 7584  # the inverter's clock minus UTC, as on 2026-11-27


def collector(entries: dict[str, list[tuple[datetime, float]]]) -> sqlite3.Connection:
    """A collector-like store: 5-minute records {series: [(UTC end, value)]}, stamped by an
    inverter clock OFFSET seconds ahead."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE log_entry (series TEXT, ts INTEGER, value REAL, read_at TEXT, UNIQUE (series, ts))")
    conn.execute("CREATE TABLE clock (read_at TEXT, inverter_ts INTEGER)")
    now = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    conn.execute("INSERT INTO clock VALUES (?, ?)", (fa.iso(now), int(now.timestamp()) + OFFSET))
    for series, rows in entries.items():
        conn.executemany("INSERT INTO log_entry VALUES (?, ?, ?, '')",
                         [(series, int(t.timestamp()) + OFFSET, v) for t, v in rows])
    return conn


def five_minutes(first: datetime, last: datetime, watts) -> list[tuple[datetime, float]]:
    """(end, W) every 5 minutes from first (exclusive start) to last, watts(end) each."""
    out, t = [], first + timedelta(minutes=5)
    while t <= last:
        out.append((t, watts(t)))
        t += timedelta(minutes=5)
    return out


class PvTest(unittest.TestCase):
    def test_hourly_energy_spread_over_four_steps(self):
        """Task 1.1: the steps sum to the forecast's energy for those hours."""
        start = datetime(2026, 11, 27, 6, tzinfo=timezone.utc)
        hourly = {start + timedelta(hours=h): 400.0 * h for h in range(50)}
        steps = hr.pv_steps(hourly, start, hr.STEPS)
        self.assertEqual(len(steps), 192)
        self.assertAlmostEqual(sum(steps), sum(400.0 * h for h in range(48)) / 1000)
        self.assertEqual(steps[4:8], [0.1] * 4)

    def test_night_counts_zero_and_beyond_the_forecast_is_missing(self):
        start = datetime(2026, 11, 27, 6, tzinfo=timezone.utc)
        hourly = {start: 1000.0, start + timedelta(hours=10): 1000.0}  # nothing listed between: night
        self.assertEqual(hr.pv_steps(hourly, start, 40)[4:8], [0.0] * 4)
        with self.assertRaises(hr.Missing):
            hr.pv_steps(hourly, start, 48)

    def test_open_meteo_fills_hours_scaled_to_forecast_solar(self):
        """Design 2: Open-Meteo's lossless estimate is scaled by forecast.solar ÷ Open-Meteo."""
        now = datetime(2026, 11, 27, 6, tzinfo=timezone.utc)
        conn = fa.connect(":memory:")
        solar = {"result": {"watt_hours_period": {fa.iso(now + timedelta(hours=h)): 800 for h in range(1, 4)}},
                 "message": {"info": {"time_utc": fa.iso(now)}}}
        times = [(now + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(1, 7)]
        meteo = {"hourly": {"time": times, "global_tilted_irradiance": [100.0] * 6, "temperature_2m": [0.0] * 6}}
        for name, body in ((fa.FORECAST_SOLAR, solar), (fa.OPEN_METEO, meteo)):
            conn.execute("INSERT INTO fetch (service, fetched_at, body) VALUES (?, ?, ?)",
                         (name, fa.iso(now), json.dumps(body).encode()))
        hourly = hr.hourly_pv(conn, now, kwp=10.0)  # Open-Meteo: 100 W/m² × 10 kWp = 1000 Wh
        self.assertEqual(hourly[now], 800)  # forecast.solar's own hour (ends at now + 1 h)
        self.assertAlmostEqual(hourly[now + timedelta(hours=4)], 800)  # Open-Meteo × 0.8

    def test_no_recent_forecast_is_missing(self):
        conn = fa.connect(":memory:")
        with self.assertRaises(hr.Missing):
            hr.hourly_pv(conn, datetime(2026, 11, 27, 6, tzinfo=timezone.utc), 10.0)


class LoadTest(unittest.TestCase):
    def test_curve_reproduces_a_known_shape(self):
        """Task 1.2: 1 kW in the morning and 3 kW in the evening, every day: the shares say so."""
        now = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
        local_start = datetime(2026, 10, 29, tzinfo=BERLIN)
        watts = lambda end: 3000.0 if (end - timedelta(minutes=5)).astimezone(BERLIN).hour >= 12 else 1000.0
        conn = collector({hr.LOG_SERIES["load"]: five_minutes(local_start.astimezone(timezone.utc), now, watts)})
        curves = hr.load_curves(conn, OFFSET, now, BERLIN)
        for kind in ("weekday", "weekend"):
            self.assertAlmostEqual(sum(curves[kind]), 1.0)
            self.assertAlmostEqual(curves[kind][0], 0.25 / (12 * 1 + 12 * 3))
            self.assertAlmostEqual(curves[kind][95], 0.75 / (12 * 1 + 12 * 3))

    def test_fit_recovers_base_temperature(self):
        """Task 1.3: daily load = 10 + 2 × max(0, 14 − t) is recovered."""
        days = [(t / 2, 10 + 2 * max(0.0, 14 - t / 2)) for t in range(-10, 50)]
        constant, slope, base = hr.fit_daily_load(days)
        self.assertAlmostEqual(constant, 10, places=6)
        self.assertAlmostEqual(slope, 2, places=6)
        self.assertEqual(base, 14)

    def test_colder_day_expects_more_with_the_same_shape(self):
        """Spec: a colder day of the same type expects more load; the curve's shape is the same."""
        conn = fa.connect(":memory:")
        now = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
        # history: 60 days, load 10 + 2 × max(0, 14 − t), t cycling through 0..14 °C
        temps = {}
        days = {}
        first = date(2026, 9, 28)
        for k in range(60):
            d = first + timedelta(days=k)
            days[d] = (10 + 2 * max(0.0, 14 - float(k % 15))) * 1000
        hour = datetime(2026, 9, 27, tzinfo=timezone.utc)
        while hour < datetime(2026, 11, 27, tzinfo=timezone.utc):  # each local day at its own temperature
            temps[hour] = float((hour.astimezone(BERLIN).date() - first).days % 15)
            hour += timedelta(hours=1)
        history = {"hourly": {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in sorted(temps)],
                              "temperature_2m": [temps[t] for t in sorted(temps)]}}
        # forecast: tomorrow 0 °C, the day after 10 °C (both Saturday and Sunday: one day type)
        fc_times = [datetime(2026, 11, 27, tzinfo=timezone.utc) + timedelta(hours=h) for h in range(72)]
        local_day = lambda t: t.astimezone(BERLIN).date()
        fc_temps = [5.0 if local_day(t) == date(2026, 11, 27) else 0.0 if local_day(t) == date(2026, 11, 28)
                    else 10.0 for t in fc_times]
        meteo = {"hourly": {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in fc_times],
                            "temperature_2m": fc_temps, "global_tilted_irradiance": [0.0] * 72}}
        conn.execute("INSERT INTO fetch (service, fetched_at, body) VALUES (?, ?, ?)",
                     (fa.HISTORY, fa.iso(now), json.dumps(history).encode()))
        conn.execute("INSERT INTO fetch (service, fetched_at, body) VALUES (?, ?, ?)",
                     (fa.OPEN_METEO, fa.iso(now), json.dumps(meteo).encode()))
        entries = {hr.LOG_SERIES["load"]: five_minutes(datetime(2026, 10, 28, tzinfo=timezone.utc), now,
                                                       lambda end: 1000.0)}
        coll = collector(entries)
        for d, wh in days.items():
            coll.execute("INSERT INTO log_entry VALUES (?, ?, ?, '')",
                         ("logger.day_eload_log_ts",
                          int(datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc).timestamp()), wh))
        start = datetime(2026, 11, 27, 23, tzinfo=timezone.utc)  # local midnight of Saturday the 28th
        loads, fit = hr.expected_load(conn, coll, OFFSET, start, 192, now, BERLIN)
        saturday, sunday = loads[:96], loads[96:]
        self.assertEqual(fit["base_temperature_c"], 14)
        self.assertGreater(sum(saturday), sum(sunday))
        self.assertAlmostEqual(sum(saturday), 10 + 2 * 14, places=3)
        ratio = sum(saturday) / sum(sunday)
        for a, b in zip(saturday, sunday):
            self.assertAlmostEqual(a, b * ratio)


class CorrectionTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 27, 11, 30, tzinfo=timezone.utc)
        self.hourly = {datetime(2026, 11, 27, h, tzinfo=timezone.utc): 1000.0 for h in range(7, 16)}

    def store(self, watts):
        midnight = datetime(2026, 11, 26, 23, tzinfo=timezone.utc)
        return collector({hr.LOG_SERIES["pv_a"]: five_minutes(midnight, self.now, watts),
                          hr.LOG_SERIES["pv_b"]: five_minutes(midnight, self.now, lambda t: 0.0)})

    def test_production_above_forecast_scales_the_rest_of_today(self):
        """Task 1.4: 1.5 times the forecast by noon gives a factor of 1.5."""
        daylight = lambda end: 1500.0 if 7 <= (end - timedelta(minutes=5)).hour < 16 else 0.0
        self.assertAlmostEqual(hr.correction_factor(self.store(daylight), OFFSET, self.hourly, self.now, BERLIN), 1.5)

    def test_below_one_kwh_nothing_is_scaled(self):
        little = lambda end: 50.0 if 7 <= (end - timedelta(minutes=5)).hour < 16 else 0.0
        self.assertIsNone(hr.correction_factor(self.store(little), OFFSET, self.hourly, self.now, BERLIN))


class BatteryTest(unittest.TestCase):
    def test_charges_up_to_the_upper_limit(self):
        """Task 2.1: surplus charges the battery to 97 %, no further; the rest is fed in."""
        grid, socs = hr.simulate([1.0] * 40, [0.0] * 40, soc=0.5)
        self.assertEqual(grid, 0.0)
        self.assertAlmostEqual(socs[-1], hr.SOC_MAX)

    def test_discharges_down_to_the_lower_limit(self):
        grid, socs = hr.simulate([0.0] * 40, [0.5] * 40, soc=0.5)
        self.assertAlmostEqual(socs[-1], hr.SOC_MIN)
        usable = (0.5 - hr.SOC_MIN) * hr.BATTERY_KWH * hr.ETA
        self.assertAlmostEqual(grid, 40 * 0.5 - usable)

    def test_power_limits(self):
        """5 kW from the battery: 2 kWh in a quarter hour need 0.75 kWh from the grid."""
        grid, _ = hr.simulate([0.0], [2.0], soc=0.9)
        self.assertAlmostEqual(grid, 2.0 - 1.25)
        grid, socs = hr.simulate([3.0], [0.0], soc=0.5)
        self.assertAlmostEqual(socs[0], 0.5 + 1.25 * hr.ETA / hr.BATTERY_KWH)


class HeadroomTest(unittest.TestCase):
    def day(self, peak_kw: float, load_kw: float):
        """24 h in 15-minute steps from local midnight: PV a half-sine from 08:00 to 16:00
        peaking at peak_kw; a flat load."""
        import math
        pv = [peak_kw * hr.DT_H * max(0.0, math.sin(math.pi * (k / 4 - 8) / 8)) if 32 <= k < 64 else 0.0
              for k in range(96 + 3)]
        return pv, [load_kw * hr.DT_H] * (96 + 3)

    def test_sunny_day_has_headroom_before_feed_in_starts(self):
        """E23: the battery would be full by early afternoon; late-morning windows already have
        headroom, before any feed-in."""
        pv, load = self.day(peak_kw=6.0, load_kw=0.3)
        _, socs = hr.simulate(pv, load, soc=0.3)
        full = next(k for k, s in enumerate(socs) if s >= hr.SOC_MAX - 1e-9)
        self.assertGreater(full, 40)  # full only after 10:00
        room = hr.headroom(pv, load, 0.3, 96)
        self.assertGreater(room[40], 0.5)  # the hour from 10:00

    def test_winter_evening_with_emptying_battery_has_none(self):
        """E24: no sun until morning and the battery runs empty: no headroom."""
        pv, load = [0.0] * 52, [0.5] * 52
        self.assertEqual(hr.headroom(pv, load, 0.4, 49), [0.0] * 49)

    def test_charge_left_at_the_horizon_is_not_free(self):
        """Review finding: without sun, a battery still charged at the end of the horizon is
        needed by the night after it, so using it is no headroom."""
        self.assertEqual(hr.headroom([0.0] * 12, [0.1] * 12, 0.9, 9), [0.0] * 9)

    def test_clear_winter_day_whose_battery_never_fills_has_none(self):
        """E26: the sun shines, but production never exceeds the load enough to fill the battery."""
        pv, load = self.day(peak_kw=1.5, load_kw=0.6)
        self.assertEqual(hr.headroom(pv, load, 0.1, 96), [0.0] * 96)


class RecordTest(unittest.TestCase):
    def test_one_record_per_hour(self):
        """Task 3.3 / E25: a run writes 192 rows; a second run in the same hour writes none."""
        conn = fa.connect(":memory:")
        made = datetime(2026, 11, 27, 12, 2, tzinfo=timezone.utc)
        steps = [{"start": fa.iso(made + k * hr.STEP), "pv_kwh": 0.1, "pv_corrected_kwh": 0.2, "load_kwh": 0.1,
                  "soc": 0.5, "headroom_kwh": 0.0, "headroom_corrected_kwh": 0.1} for k in range(192)]
        self.assertEqual(hr.record(conn, {"made_at": fa.iso(made), "steps": steps}), 192)
        later = fa.iso(made + timedelta(minutes=15))
        self.assertEqual(hr.record(conn, {"made_at": later, "steps": steps}), 0)
        self.assertEqual(conn.execute("SELECT count(*) FROM headroom_forecast").fetchone()[0], 192)


def world(now: datetime, pv_today_w: float = 0.0):
    """An archive and a collector store as the archive would see them at `now`: 60 days of
    history (load 10 + 2 × max(0, 14 − t) kWh, t cycling 0..14 °C by local day), 5-minute load
    records at a flat 1 kW, a state of charge of 50 % an hour ago, forecast.solar for today and
    tomorrow (UTC, 800 Wh in each hour 08–15) and Open-Meteo for 4 days (GTI 100 W/m², 5 °C)."""
    arch = fa.connect(":memory:")
    first = (now - timedelta(days=60)).date()
    temps, hour = {}, datetime(first.year, first.month, first.day, tzinfo=timezone.utc) - timedelta(days=1)
    while hour < now.replace(hour=0, minute=0):
        temps[hour] = float((hour.astimezone(BERLIN).date() - first).days % 15)
        hour += timedelta(hours=1)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    om_times = [day0 + timedelta(hours=h) for h in range(96)]
    solar = {"result": {"watt_hours_period": {fa.iso(day0 + timedelta(days=d, hours=h + 1)): 800
                                              for d in range(2) for h in range(8, 16)}},
             "message": {"info": {"time_utc": fa.iso(now)}}}
    bodies = {
        fa.HISTORY: {"hourly": {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in sorted(temps)],
                                "temperature_2m": [temps[t] for t in sorted(temps)]}},
        fa.OPEN_METEO: {"hourly": {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in om_times],
                                   "temperature_2m": [5.0] * 96,
                                   # labelled by the hour's end: the hours 08-15 as forecast.solar's
                                   "global_tilted_irradiance": [100.0 if 9 <= t.hour < 17 else 0.0 for t in om_times]}},
        fa.FORECAST_SOLAR: solar,
    }
    for name, body in bodies.items():
        arch.execute("INSERT INTO fetch (service, fetched_at, body) VALUES (?, ?, ?)",
                     (name, fa.iso(now - timedelta(minutes=30)), json.dumps(body).encode()))
    local_midnight = datetime.combine(now.astimezone(BERLIN).date(), datetime.min.time(), BERLIN)
    daylight = lambda end: pv_today_w if 8 <= (end - timedelta(minutes=5)).hour < 16 else 0.0
    coll = collector({
        hr.LOG_SERIES["load"]: five_minutes(now - timedelta(days=40), now - timedelta(hours=1), lambda end: 1000.0),
        hr.LOG_SERIES["soc"]: [(now - timedelta(hours=1), 0.5)],
        hr.LOG_SERIES["pv_a"]: five_minutes(local_midnight.astimezone(timezone.utc), now - timedelta(hours=1), daylight),
        hr.LOG_SERIES["pv_b"]: five_minutes(local_midnight.astimezone(timezone.utc), now - timedelta(hours=1),
                                            lambda end: 0.0),
    })
    for k in range(60):
        d = first + timedelta(days=k)
        coll.execute("INSERT INTO log_entry VALUES (?, ?, ?, '')",
                     ("logger.day_eload_log_ts", int(datetime(d.year, d.month, d.day, 23, 59, 59,
                                                              tzinfo=timezone.utc).timestamp()),
                      (10 + 2 * max(0.0, 14 - float(k % 15))) * 1000))
    return arch, coll


class ForecastTest(unittest.TestCase):
    def test_late_evening_forecast_is_complete(self):
        """Review finding: at 22:30 UTC the 48 h reach into the fourth UTC day of Open-Meteo."""
        now = datetime(2026, 11, 27, 22, 30, tzinfo=timezone.utc)
        result = hr.forecast(*world(now), now, 7.92, BERLIN)
        steps = result["steps"]
        self.assertEqual(len(steps), 192)
        self.assertEqual(steps[0]["start"], "2026-11-27T22:30:00+00:00")
        self.assertEqual(steps[-1]["start"], "2026-11-29T22:15:00+00:00")
        night = [s["pv_kwh"] for s in steps if fa.parse_time(s["start"]).hour in (0, 1, 2)]
        self.assertTrue(night and all(p == 0 for p in night))

    def test_correction_applies_to_the_rest_of_today_only(self):
        """Spec "Corrected and uncorrected PV side by side": today's later steps scaled, tomorrow not."""
        now = datetime(2026, 11, 27, 12, 30, tzinfo=timezone.utc)
        result = hr.forecast(*world(now, pv_today_w=1200.0), now, 7.92, BERLIN)
        self.assertAlmostEqual(result["correction_factor"], 1.5, places=2)  # 1200 W against 800 Wh per hour
        for s in result["steps"]:
            today = fa.parse_time(s["start"]).astimezone(BERLIN).date() == now.astimezone(BERLIN).date()
            expected = s["pv_kwh"] * 1.5 if today else s["pv_kwh"]
            self.assertAlmostEqual(s["pv_corrected_kwh"], round(expected, 3), places=2)


class EndpointTest(unittest.TestCase):
    """Task 3.2 / spec "An input is missing" (E2): the reason, and no steps."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name, "forecasts.sqlite"))
        fa.connect(self.db).close()

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, collector_db):
        return fa.headroom_payload(self.db, collector_db, datetime(2026, 11, 27, 12, tzinfo=timezone.utc),
                                   7.92, BERLIN)

    def test_no_collector_store(self):
        p = self.payload(str(Path(self.tmp.name, "none.sqlite")))
        self.assertEqual((p["steps"], p["missing"]), ([], "the collector's store is not available"))

    def test_unreadable_store(self):
        path = str(Path(self.tmp.name, "other.sqlite"))
        sqlite3.connect(path).execute("CREATE TABLE other (x)").connection.close()
        p = self.payload(path)
        self.assertEqual(p["steps"], [])
        self.assertIn("cannot be read", p["missing"])

    def test_stale_state_of_charge(self):
        path = str(Path(self.tmp.name, "collector.sqlite"))
        conn = collector({hr.LOG_SERIES["soc"]: [(datetime(2026, 11, 27, 9, tzinfo=timezone.utc), 0.5)]})
        conn.commit()
        conn.execute("VACUUM INTO ?", (path,))
        p = self.payload(path)
        self.assertEqual(p["steps"], [])
        self.assertIn("state of charge", p["missing"])

    def test_served_over_http(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        handler = fa.make_handler(self.db, 7.92, collector_db=str(Path(self.tmp.name, "none.sqlite")))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/headroom") as response:
                self.assertEqual(json.load(response)["steps"], [])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
