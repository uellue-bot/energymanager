"""Tests for archive/forecast_archive.py (specs: forecast-archive, satellite-radiation-archive;
examples E5, E6 and E13).

The responses are synthetic: real ones contain the caller's public IP address
and place name, so they must not be committed.
"""

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "archive"))
import forecast_archive as fa  # noqa: E402

T0 = datetime(2026, 11, 2, 0, 0, tzinfo=timezone.utc)


def solar_body(now: datetime) -> bytes:
    """A forecast.solar-like response computed at `now`: 500 Wh in each hour of the next two days."""
    hours = [now.replace(minute=0, second=0) + timedelta(hours=h) for h in range(1, 49)]
    return json.dumps({
        "result": {"watt_hours_period": {fa.iso(t): 500 for t in hours}},
        "message": {"info": {"time_utc": fa.iso(now)}},
    }).encode()


def weather_body(now: datetime) -> bytes:
    """An Open-Meteo-like response: GTI 100 W/m² in each hour of three days."""
    day = now.replace(hour=0, minute=0, second=0)
    times = [(day + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(72)]
    return json.dumps({"hourly": {"time": times, "global_tilted_irradiance": [100.0] * 72}}).encode()


def satellite_body(now: datetime, offset: float = 0.0, missing=(), today: bool = False) -> bytes:
    """A satellite-API-like response in the shape recorded on 2026-11-23 (neutral coordinates,
    synthetic values): the 24 hours of the day before `now`, labelled 00:00 to 23:00.

    Each quantity is the label's index plus `offset` (the hour of day for the
    first day, 24 more for the second), with a quantity-specific step, so that
    every value is distinct; labels in `missing` have null values.
    With `today`, the response also holds the 24 hours of `now`'s day, as with
    past_days=1 and forecast_days=1; the labels from the hour of `now` on are
    null, since the satellite has not observed them yet.
    """
    day = now.replace(hour=0, minute=0, second=0) - timedelta(days=1)
    labels = [day + timedelta(hours=h) for h in range(48 if today else 24)]
    times = [t.strftime("%Y-%m-%dT%H:%M") for t in labels]
    observed = now.replace(minute=0, second=0, microsecond=0)
    hourly = {"time": times}
    for k, name in enumerate(fa.SATELLITE_VARIABLES):
        hourly[name] = [None if t in missing or label >= observed else 1000.0 * k + h + offset
                        for h, (t, label) in enumerate(zip(times, labels))]
    return json.dumps({
        "latitude": 52.52, "longitude": 13.41, "generationtime_ms": 0.1, "utc_offset_seconds": 0,
        "timezone": "GMT", "timezone_abbreviation": "GMT", "elevation": 38.0,
        "hourly_units": {"time": "iso8601", **{name: "W/m²" for name in fa.SATELLITE_VARIABLES}},
        "hourly": hourly,
    }).encode()


class FakeServices:
    """A local HTTP server standing in for the three services. It records each request."""

    def __init__(self):
        self.now = T0
        self.status = {"/solar": 200, "/weather": 200, "/satellite": 200}
        self.satellite_offset = 0.0
        self.satellite_today = False
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.requests.append(self.path)
                body = {"/solar": lambda: solar_body(fake.now),
                        "/weather": lambda: weather_body(fake.now),
                        "/satellite": lambda: satellite_body(fake.now, fake.satellite_offset,
                                                             today=fake.satellite_today)}[self.path]()
                self.send_response(fake.status[self.path])
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.services = [
            fa.Service(fa.FORECAST_SOLAR, base + "/solar", fa.forecast_solar_issued_at),
            fa.Service(fa.OPEN_METEO, base + "/weather", fa.no_issue_time),
            fa.Service(fa.SATELLITE, base + "/satellite", fa.no_issue_time),
        ]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name, "forecasts.sqlite"))
        self.conn = fa.connect(self.db)
        self.fake = FakeServices()

    def tearDown(self):
        self.fake.close()
        self.conn.close()
        self.tmp.cleanup()

    def run_hours(self, hours, minute=2):
        for h in range(hours):
            self.fake.now = T0 + timedelta(hours=h, minutes=minute)
            fa.fetch_all(self.conn, self.fake.services, self.fake.now)

    def count(self, service, where="1"):
        return self.conn.execute(
            f"SELECT count(*) FROM fetch WHERE service = ? AND {where}", (service,)).fetchone()[0]


class ArchiveTest(ArchiveCase):
    def test_a_day_of_fetches(self):
        self.run_hours(24)
        for service in (fa.FORECAST_SOLAR, fa.OPEN_METEO):
            self.assertEqual(self.count(service, "error IS NULL"), 24)

    def test_restart_loop_calls_once_per_hour(self):
        for minute in (2, 10, 20, 40, 59):
            now = T0 + timedelta(minutes=minute)
            fa.fetch_all(self.conn, self.fake.services, now)
        self.assertEqual(sorted(self.fake.requests), ["/satellite", "/solar", "/weather"])

    def test_service_down_is_recorded(self):
        self.fake.status["/solar"] = 503
        self.run_hours(1)
        row = self.conn.execute(
            "SELECT http_status, error FROM fetch WHERE service = ?", (fa.FORECAST_SOLAR,)).fetchone()
        self.assertEqual(row, (503, "HTTP 503"))
        self.assertIsNone(fa.latest_before(self.conn, fa.FORECAST_SOLAR, T0 + timedelta(hours=1)))
        self.assertEqual(self.count(fa.OPEN_METEO, "error IS NULL"), 1)

    def test_unreachable_service_is_recorded(self):
        self.fake.close()
        fa.fetch_all(self.conn, self.fake.services, T0)
        errors = [r[0] for r in self.conn.execute("SELECT error FROM fetch")]
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(e.startswith("URLError") for e in errors), errors)
        self.fake = FakeServices()

    def test_kept_unchanged_with_issue_time(self):
        self.run_hours(1)
        issue = fa.latest_before(self.conn, fa.FORECAST_SOLAR, T0 + timedelta(hours=1))
        self.assertEqual(issue["body"], solar_body(T0 + timedelta(minutes=2)))
        self.assertEqual(issue["issued_at"], "2026-11-02T00:02:00+00:00")
        weather = fa.latest_before(self.conn, fa.OPEN_METEO, T0 + timedelta(hours=1))
        self.assertIsNone(weather["issued_at"])

    def test_archive_is_append_only(self):
        self.run_hours(1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM fetch")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE fetch SET body = NULL")

    def test_e6_lead_times_over_a_week(self):
        """E6: for an hour of the week, the forecasts issued 1, 6 and 24 h before it."""
        self.run_hours(7 * 24)
        hour = T0 + timedelta(days=5, hours=12)
        for lead in (1, 6, 24):
            for service in (fa.FORECAST_SOLAR, fa.OPEN_METEO):
                issue = fa.latest_before(self.conn, service, hour - timedelta(hours=lead))
                expected = hour - timedelta(hours=lead) - timedelta(minutes=58)
                self.assertEqual(issue["fetched_at"], fa.iso(expected), (service, lead))

    def test_e5_series(self):
        self.run_hours(3 * 24)
        now = T0 + timedelta(days=3)
        series = fa.e5_series(self.conn, now, days=2, kwp=7.92)
        self.assertEqual(len(series), 48)
        first = series[0]
        self.assertEqual(first["start"], fa.iso(T0 + timedelta(days=1)))
        self.assertEqual(first["solar_forecast_wh"], 500)
        self.assertAlmostEqual(first["weather_estimate_wh"], 792.0)
        self.assertEqual(first["solar_forecast_fetched_at"], fa.iso(T0 + timedelta(hours=23, minutes=2)))

    def test_endpoint(self):
        self.run_hours(2)
        handler = fa.make_handler(self.db, 7.92, clock=lambda: T0 + timedelta(hours=3))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/e5?days=1"
            with urllib.request.urlopen(url) as response:
                hours = json.load(response)["hours"]
            self.assertEqual(len(hours), 24)
            self.assertEqual(hours[-1]["start"], fa.iso(T0 + timedelta(hours=2)))
            self.assertEqual(hours[-1]["solar_forecast_wh"], 500)
        finally:
            server.shutdown()
            server.server_close()


class SatelliteTest(ArchiveCase):
    """E13: Open-Meteo's satellite irradiance of the past day, fetched hourly and kept as fetched
    (spec: satellite-radiation-archive)."""

    def test_a_day_of_fetches(self):
        """E13: 24 responses a day; together they give all four quantities for every hour of a day."""
        self.run_hours(48)
        self.assertEqual(self.count(fa.SATELLITE, "error IS NULL"), 48)
        day = T0 - timedelta(days=1)
        for h in range(24):
            hour = fa.satellite_hour(self.conn, day + timedelta(hours=h))
            self.assertIsNotNone(hour, h)
            self.assertEqual(set(hour) - {"start", "fetched_at"}, set(fa.SATELLITE_VARIABLES))
            self.assertTrue(all(hour[name] is not None for name in fa.SATELLITE_VARIABLES), h)

    def test_a_day_of_fetches_with_todays_null_tail(self):
        """E13, in the shape forecast_days=1 returns: yesterday and today, today null from the current
        hour on. Hourly fetches over two days give all four quantities for every hour of the first
        day; an hour not yet observed reads as None, not as a value of an older day."""
        self.fake.satellite_today = True
        self.run_hours(48)  # 2026-11-02 00:02 to 2026-11-03 23:02
        for h in range(24):
            hour = fa.satellite_hour(self.conn, T0 + timedelta(hours=h))
            self.assertTrue(all(hour[name] is not None for name in fa.SATELLITE_VARIABLES), h)
            self.assertEqual(hour["shortwave_radiation"], h + 1, h)  # 23:00-24:00 is today's 00:00 label
        # Observed during the day: 2026-11-03 21:00-22:00 is labelled 22:00, today's entry in the last
        # fetch at 23:02 (index 46), not yesterday's 22:00 (index 22).
        hour = fa.satellite_hour(self.conn, T0 + timedelta(days=1, hours=21))
        self.assertEqual((hour["fetched_at"], hour["shortwave_radiation"]),
                         (fa.iso(T0 + timedelta(days=1, hours=23, minutes=2)), 46.0))
        # Not yet observed: 22:00-23:00 is labelled 23:00, null in every response that holds it.
        hour = fa.satellite_hour(self.conn, T0 + timedelta(days=1, hours=22))
        self.assertEqual([hour[name] for name in fa.SATELLITE_VARIABLES], [None] * 4)

    def test_hour_null_in_every_response(self):
        """An hour that every response holds as null reads as None, from the newest response."""
        body = satellite_body(T0, missing={"2026-11-01T13:00"})
        for fetched_at in (T0, T0 + timedelta(hours=1)):
            self.conn.execute("INSERT INTO fetch (service, fetched_at, http_status, body) VALUES (?, ?, 200, ?)",
                              (fa.SATELLITE, fa.iso(fetched_at), body))
        hour = fa.satellite_hour(self.conn, datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
        self.assertEqual(hour["fetched_at"], fa.iso(T0 + timedelta(hours=1)))
        self.assertEqual([hour[name] for name in fa.SATELLITE_VARIABLES], [None] * 4)

    def test_kept_unchanged_without_issue_time(self):
        """E13: the response is kept byte for byte; the service states no issue time."""
        self.run_hours(1)
        kept = fa.latest_before(self.conn, fa.SATELLITE, T0 + timedelta(hours=1))
        self.assertEqual(kept["body"], satellite_body(T0 + timedelta(minutes=2)))
        self.assertIsNone(kept["issued_at"])

    def test_satellite_down_forecasts_kept(self):
        """E13: a failed satellite fetch is recorded; the forecasts of that hour are kept as usual."""
        self.fake.status["/satellite"] = 503
        self.run_hours(1)
        row = self.conn.execute(
            "SELECT http_status, error FROM fetch WHERE service = ?", (fa.SATELLITE,)).fetchone()
        self.assertEqual(row, (503, "HTTP 503"))
        for service in (fa.FORECAST_SOLAR, fa.OPEN_METEO):
            self.assertEqual(self.count(service, "error IS NULL"), 1)
        self.assertIsNone(fa.satellite_hour(self.conn, T0 - timedelta(hours=12)))

    def test_next_hour_after_failure(self):
        """E13: after a failed fetch, the next hour tries again."""
        self.fake.status["/satellite"] = 503
        self.run_hours(1)
        self.fake.status["/satellite"] = 200
        self.fake.now = T0 + timedelta(hours=1, minutes=2)
        self.assertEqual(fa.fetch_all(self.conn, self.fake.services, self.fake.now)[fa.SATELLITE], "ok")

    def test_later_response_revises_an_hour(self):
        """E13: a later response with a different value is the one read; both stay kept."""
        self.run_hours(1)
        self.fake.satellite_offset = 0.5
        self.fake.now = T0 + timedelta(hours=1, minutes=2)
        fa.fetch_all(self.conn, self.fake.services, self.fake.now)
        hour = fa.satellite_hour(self.conn, T0 - timedelta(hours=12))  # 12:00-13:00, labelled 13:00
        self.assertEqual(hour, {
            "start": fa.iso(T0 - timedelta(hours=12)),
            "fetched_at": fa.iso(T0 + timedelta(hours=1, minutes=2)),
            "shortwave_radiation": 13.5, "direct_normal_irradiance": 1013.5,
            "diffuse_radiation": 2013.5, "global_tilted_irradiance": 3013.5,
        })
        earlier = fa.latest_before(self.conn, fa.SATELLITE, T0 + timedelta(hours=1))
        self.assertEqual(json.loads(earlier["body"])["hourly"]["shortwave_radiation"][13], 13.0)

    def test_missing_value_and_uncovered_hour(self):
        """A null from the service reads as None; an hour no response covers reads as None."""
        body = satellite_body(T0, missing={"2026-11-01T13:00"})
        self.conn.execute("INSERT INTO fetch (service, fetched_at, http_status, body) VALUES (?, ?, 200, ?)",
                          (fa.SATELLITE, fa.iso(T0), body))
        hour = fa.satellite_hour(self.conn, datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
        self.assertEqual([hour[name] for name in fa.SATELLITE_VARIABLES], [None] * 4)
        self.assertIsNone(fa.satellite_hour(self.conn, T0 + timedelta(hours=1)))

    def test_newer_response_not_yet_observed_falls_back_to_older(self):
        """E13: a newer response holding only nulls for an hour (fetched before the satellite had it)
        does not hide an older response's values for that hour."""
        older = satellite_body(T0)
        newer = satellite_body(T0, offset=0.5, missing={"2026-11-01T13:00"})
        for fetched_at, body in ((T0, older), (T0 + timedelta(minutes=30), newer)):
            self.conn.execute("INSERT INTO fetch (service, fetched_at, http_status, body) VALUES (?, ?, 200, ?)",
                              (fa.SATELLITE, fa.iso(fetched_at), body))
        hour = fa.satellite_hour(self.conn, datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
        self.assertEqual((hour["fetched_at"], hour["shortwave_radiation"]), (fa.iso(T0), 13.0))
        hour = fa.satellite_hour(self.conn, datetime(2026, 11, 1, 11, tzinfo=timezone.utc))
        self.assertEqual((hour["fetched_at"], hour["shortwave_radiation"]), (fa.iso(T0 + timedelta(minutes=30)), 12.5))

    def test_last_hour_of_a_day_comes_with_the_next_day(self):
        """The hour 23:00-24:00 is labelled 00:00 of the next day, so a response of the previous day
        does not cover it; a naive time is read as UTC."""
        self.run_hours(24)  # fetches on 2026-11-02, each holding the labels of 2026-11-01
        self.assertIsNone(fa.satellite_hour(self.conn, datetime(2026, 11, 1, 23, tzinfo=timezone.utc)))
        hour = fa.satellite_hour(self.conn, datetime(2026, 11, 1, 22))
        self.assertEqual((hour["start"], hour["shortwave_radiation"]), ("2026-11-01T22:00:00+00:00", 23.0))

    def test_url_from_env(self):
        """The query names the four quantities, the roof plane from .env, the satellite product alone
        (no forecast filling of hours not yet observed), yesterday and today, UTC (E13)."""
        env = {"EM_LATITUDE": "52.52", "EM_LONGITUDE": "13.41", "EM_DECLINATION": "30",
               "EM_AZIMUTH": "-10", "EM_KWP": "5"}
        service = {s.name: s for s in fa.services_from_env(env)}[fa.SATELLITE]
        url = urlsplit(service.url)
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", "https://satellite-api.open-meteo.com/v1/archive")
        self.assertEqual(parse_qs(url.query), {
            "latitude": ["52.52"], "longitude": ["13.41"],
            "hourly": [",".join(fa.SATELLITE_VARIABLES)], "tilt": ["30"], "azimuth": ["-10"],
            "past_days": ["1"], "forecast_days": ["1"], "timezone": ["UTC"],
            "models": ["dwd_sis_europe_africa_v4"]})
        self.assertIs(service.issued_at, fa.no_issue_time)


class Helpers(unittest.TestCase):
    def test_forecast_solar_partial_periods_go_to_the_hour_they_end_in(self):
        body = json.dumps({"result": {"watt_hours_period": {
            "2026-11-21T07:00:05+00:00": 0, "2026-11-21T08:00:00+00:00": 120,
            "2026-11-21T15:40:24+00:00": 30}}}).encode()
        hourly = fa.forecast_solar_hourly_wh(body)
        start = datetime(2026, 11, 21, tzinfo=timezone.utc)
        self.assertEqual(hourly[start.replace(hour=7)], 120)
        self.assertEqual(hourly[start.replace(hour=15)], 30)

    def test_next_run(self):
        now = datetime(2026, 11, 21, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(fa.seconds_until_next_run(now), 32 * 60)


if __name__ == "__main__":
    unittest.main()
