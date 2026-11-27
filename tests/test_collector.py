"""Tests for collector/collector.py (changes collect-device-details and collect-pv-health-inputs,
examples E2, E10, E11, E12 and E14).

The inverter is a local TCP server that answers frames like the real one.
Needs rctclient (collector/Dockerfile); skipped without it, e.g.
`docker compose run --rm --no-deps -v "$PWD:/repo:ro" collector python3 -m unittest discover -s /repo/tests -p test_collector.py`.
"""

import socket
import struct
import sqlite3
import sys
import tempfile
import threading
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

try:
    from rctclient.frame import ReceiveFrame, make_frame
    from rctclient.registry import REGISTRY
    from rctclient.types import Command
    from rctclient.utils import encode_value
except ImportError:
    REGISTRY = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collector"))
if REGISTRY is not None:
    import collector as co  # noqa: E402

T0 = datetime(2026, 12, 19, 10, 0, tzinfo=timezone.utc)


class FakeInverter:
    """Answers READ frames from `values` (object name -> value).

    `cut` names objects whose answer is cut in half `cut_times` times, the way
    the real inverter answers when another client interrupts it. `silent` names
    objects that get no answer at all.
    """

    def __init__(self, values, cut=(), cut_times=1, logs=None, chunk=3, silent=()):
        self.values = values
        self.silent = set(silent)
        self.logs = logs or {}  # series name -> [(ts, value)]
        self.chunk = chunk
        self.cut = set(cut)
        self.cut_left = {name: cut_times for name in cut}
        self.commands = []
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            with client:
                frame, buffer = ReceiveFrame(), b""
                while True:
                    if not buffer:
                        buffer = client.recv(1024)
                        if not buffer:
                            break
                    used = frame.consume(buffer)
                    buffer = buffer[used:]
                    if not frame.complete():
                        continue
                    oinfo = REGISTRY.get_by_id(frame.id)
                    self.commands.append((frame.command, oinfo.name))
                    if oinfo.name in self.silent:
                        frame = ReceiveFrame()
                        continue
                    if frame.command == Command.WRITE:  # a log query: entries before the timestamp
                        before = int.from_bytes(frame.data, "big")
                        older = [e for e in self.logs.get(oinfo.name, []) if e[0] < before][-self.chunk:]
                        payload = struct.pack(">I", before) + b"".join(struct.pack(">If", ts, v) for ts, v in older)
                    else:
                        payload = encode_value(oinfo.response_data_type, self.values[oinfo.name])
                    answer = make_frame(command=Command.RESPONSE, id=frame.id, payload=payload)
                    if self.cut_left.get(oinfo.name, 0) > 0:
                        self.cut_left[oinfo.name] -= 1
                        client.sendall(answer[: len(answer) // 2])
                        break
                    client.sendall(answer)
                    frame = ReceiveFrame()

    def close(self):
        self.server.close()


STATE_VALUES = {
    "power_mng.state": 3,
    "battery.bat_status": 1032,
    "battery.status": 0,
    "battery.status2": 0,
    "prim_sm.state": 6,
    "dc_conv.last_rescan": 1_795_000_000,
    "dc_conv.dc_conv_struct[0].rescan_correction": -3.5,
    "dc_conv.dc_conv_struct[0].u_target": 412.5,
}
MPP_OBJECTS = ("dc_conv.last_rescan", "dc_conv.dc_conv_struct[0].rescan_correction",
               "dc_conv.dc_conv_struct[0].u_target")


@unittest.skipIf(REGISTRY is None, "rctclient is not installed")
class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = co.connect(str(Path(self.tmp.name) / "c.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def rows(self, table):
        return self.conn.execute(f"SELECT * FROM {table}").fetchall()

    def test_calibration_recorded_raw(self):
        """E10: every state object is stored raw, e.g. bat_status 1032 (charging and discharging bits)."""
        fake = FakeInverter(STATE_VALUES)
        self.addCleanup(fake.close)
        stored = co.read_state(self.conn, lambda: co.Inverter("127.0.0.1", fake.port), T0, pause=lambda s: None)
        self.assertEqual(stored, {k: float(v) for k, v in STATE_VALUES.items()})
        self.assertEqual(len(self.rows("state")), 8)
        self.assertEqual(self.rows("failure"), [])
        self.assertEqual({c for c, _ in fake.commands}, {Command.READ})

    def test_mpp_objects_every_minute(self):
        """E14: the three MPP objects are read every minute with the others, raw; a rescan shows as a
        new last-rescan time with the correction and target of the same minute. Only reads are sent."""
        fake = FakeInverter(dict(STATE_VALUES))
        self.addCleanup(fake.close)
        inverter = lambda: co.Inverter("127.0.0.1", fake.port)  # noqa: E731
        co.read_state(self.conn, inverter, T0, pause=lambda s: None)
        fake.values.update({"dc_conv.last_rescan": 1_795_000_050,
                            "dc_conv.dc_conv_struct[0].rescan_correction": 7.25,
                            "dc_conv.dc_conv_struct[0].u_target": 405.0})
        minute = T0.replace(minute=1)
        co.read_state(self.conn, inverter, minute, pause=lambda s: None)
        rows = self.conn.execute("SELECT read_at, object, value FROM state WHERE object IN (?, ?, ?) "
                                 "ORDER BY read_at, object", MPP_OBJECTS).fetchall()
        self.assertEqual(rows, [
            (co.iso(T0), "dc_conv.dc_conv_struct[0].rescan_correction", -3.5),
            (co.iso(T0), "dc_conv.dc_conv_struct[0].u_target", 412.5),
            (co.iso(T0), "dc_conv.last_rescan", 1_795_000_000.0),
            (co.iso(minute), "dc_conv.dc_conv_struct[0].rescan_correction", 7.25),
            (co.iso(minute), "dc_conv.dc_conv_struct[0].u_target", 405.0),
            (co.iso(minute), "dc_conv.last_rescan", 1_795_000_050.0),
        ])
        self.assertEqual(self.rows("failure"), [])
        self.assertEqual({c for c, _ in fake.commands}, {Command.READ})
        self.assertEqual(co.status(self.conn)["last_success"]["state"], co.iso(minute))

    def test_mpp_object_cut_twice_recorded(self):
        """E14, E2: an MPP object cut on the retry too has no value for the minute and a failure entry;
        the other seven are stored."""
        target = "dc_conv.dc_conv_struct[0].u_target"
        fake = FakeInverter(STATE_VALUES, cut=[target], cut_times=2)
        self.addCleanup(fake.close)
        stored = co.read_state(self.conn, lambda: co.Inverter("127.0.0.1", fake.port, timeout=1), T0,
                               pause=lambda s: None)
        self.assertEqual(set(stored), set(STATE_VALUES) - {target})
        failures = self.rows("failure")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][1], "state")
        self.assertTrue(failures[0][2].startswith(target + ":"), failures[0][2])

    def test_mpp_nan_at_night_is_a_gap_not_an_alert(self):
        """E14, night (assumed): an MPP object answering NaN leaves a gap, records no failure, and the
        state still counts as read, so the "state not read" alert stays quiet."""
        # The two float objects; the last rescan is an integer and cannot be NaN. NaN used to break
        # the minute's insert (the value column is NOT NULL) and lose the other objects' values.
        nan = {n: float("nan") for n in co.MPP_OBJECTS if n != "dc_conv.last_rescan"}
        fake = FakeInverter(dict(STATE_VALUES, **nan))
        self.addCleanup(fake.close)
        stored = co.read_state(self.conn, lambda: co.Inverter("127.0.0.1", fake.port), T0, pause=lambda s: None)
        self.assertEqual(set(stored), set(STATE_VALUES) - set(nan))
        self.assertEqual(self.rows("failure"), [])
        self.assertEqual(co.status(self.conn)["last_success"]["state"], co.iso(T0))

    def test_mpp_no_answer_is_a_failure_not_an_alert(self):
        """E14, review of 2026-11-23: an MPP object that gets no answer, even on the retry, is recorded
        as a failure, unlike a NaN; the core objects are stored and the state still counts as read.
        Whether the inverter does this at night is checked after the test deploy (task 3.2)."""
        target = "dc_conv.dc_conv_struct[0].u_target"
        fake = FakeInverter(STATE_VALUES, silent=[target])
        self.addCleanup(fake.close)
        stored = co.read_state(self.conn, lambda: co.Inverter("127.0.0.1", fake.port, timeout=1), T0,
                               pause=lambda s: None)
        self.assertEqual(set(stored), set(STATE_VALUES) - {target})
        failures = self.rows("failure")
        self.assertEqual([(r[1], r[2].split(":")[0]) for r in failures], [("state", target)])
        self.assertEqual(co.status(self.conn)["last_success"]["state"], co.iso(T0))

    def test_core_nan_is_a_failure_not_a_lost_minute(self):
        """A core object answering NaN is recorded as a failure, the other objects of the minute are
        stored, and the state does not count as read. The core objects are integers, so the inverter
        cannot send NaN for them; the read is patched to guard the branch anyway."""
        target = "power_mng.state"
        fake = FakeInverter(STATE_VALUES)
        self.addCleanup(fake.close)
        real_read = co.Inverter.read
        nan_read = lambda inv, name: float("nan") if name == target else real_read(inv, name)  # noqa: E731
        with unittest.mock.patch.object(co.Inverter, "read", nan_read):
            stored = co.read_state(self.conn, lambda: co.Inverter("127.0.0.1", fake.port), T0,
                                   pause=lambda s: None)
        self.assertEqual(set(stored), set(STATE_VALUES) - {target})
        self.assertEqual([(r[1], r[2]) for r in self.rows("failure")], [("state", f"{target}: value is NaN")])
        self.assertIsNone(co.status(self.conn)["last_success"]["state"])

    def test_cut_frame_retried(self):
        """A cut answer is retried once, after a pause, over a new connection; the value is then stored."""
        fake = FakeInverter(STATE_VALUES, cut=["power_mng.state"])
        self.addCleanup(fake.close)
        pauses = []
        stored = co.read_state(self.conn, lambda: co.Inverter("127.0.0.1", fake.port, timeout=1), T0,
                               pause=pauses.append)
        self.assertEqual(pauses, [co.RETRY_PAUSE_S])
        self.assertEqual(stored, {k: float(v) for k, v in STATE_VALUES.items()})
        self.assertEqual(self.rows("failure"), [])

    def test_cut_twice_recorded(self):
        """Cut on the retry too: that object has no value for the minute (E2), the others do."""
        fake = FakeInverter(STATE_VALUES, cut=["battery.status"], cut_times=2)
        self.addCleanup(fake.close)
        stored = co.read_state(self.conn, lambda: co.Inverter("127.0.0.1", fake.port, timeout=1), T0,
                               pause=lambda s: None)
        self.assertNotIn("battery.status", stored)
        self.assertEqual(len(stored), 7)
        self.assertEqual([r[1] for r in self.rows("failure")], ["state"])

    def test_unreachable_is_a_gap(self):
        """E2: an unreachable inverter leaves no value for that minute and records the failure."""
        with socket.create_server(("127.0.0.1", 0)) as s:  # a port nobody listens on afterwards
            port = s.getsockname()[1]
        stored = co.read_state(self.conn, lambda: co.Inverter("127.0.0.1", port, timeout=1), T0,
                               pause=lambda s: None)
        self.assertEqual(stored, {})
        self.assertEqual(self.rows("state"), [])
        failures = self.rows("failure")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][1], "state")

    def test_append_only(self):
        fake = FakeInverter(STATE_VALUES)
        self.addCleanup(fake.close)
        co.read_state(self.conn, lambda: co.Inverter("127.0.0.1", fake.port), T0, pause=lambda s: None)
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM state")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE state SET value = 0")

    def test_state_success_needs_every_object(self):
        """One object failing every minute keeps the state from counting as read (review finding 6)."""
        fake = FakeInverter(STATE_VALUES, cut=["power_mng.state"], cut_times=99)
        self.addCleanup(fake.close)
        co.read_state(self.conn, lambda: co.Inverter("127.0.0.1", fake.port, timeout=1), T0, pause=lambda s: None)
        self.assertIsNone(co.status(self.conn)["last_success"]["state"])

    def test_status_endpoint_data(self):
        fake = FakeInverter(STATE_VALUES)
        self.addCleanup(fake.close)
        co.read_state(self.conn, lambda: co.Inverter("127.0.0.1", fake.port), T0, pause=lambda s: None)
        s = co.status(self.conn)
        self.assertEqual(s["state"]["power_mng.state"]["value"], 3.0)
        self.assertEqual(s["last_success"]["state"], co.iso(T0))


SOC = "logger.minutes_soc_log_ts"
NOW_TS = int(T0.timestamp())
SOC_LOG = [(NOW_TS - 300 * k, 0.5 + k / 100) for k in range(7, 0, -1)]  # 7 entries, oldest first


@unittest.skipIf(REGISTRY is None, "rctclient is not installed")
class LogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "c.sqlite")
        self.conn = co.connect(self.path)
        self.fake = FakeInverter({"flash_rtc.time_stamp": NOW_TS}, logs={SOC: SOC_LOG}, chunk=3)
        self.inv = lambda: co.Inverter("127.0.0.1", self.fake.port)

    def tearDown(self):
        self.fake.close()
        self.conn.close()
        self.tmp.cleanup()

    def entries(self):
        return self.conn.execute("SELECT ts, value FROM log_entry WHERE series = ? ORDER BY ts", (SOC,)).fetchall()

    def test_walk_to_the_end(self):
        """E11: the walk goes back until the inverter returns nothing older, then is marked done."""
        steps = []
        while SOC in co.walk_pending(self.conn):
            steps.append(co.walk_step(self.conn, self.inv, SOC, T0, pause=lambda s: None))
        self.assertEqual(steps, [3, 3, 1, 0])
        self.assertEqual([(ts, round(v, 4)) for ts, v in self.entries()], [(ts, round(v, 4)) for ts, v in SOC_LOG])

    def test_empty_cut_answer_does_not_end_the_walk(self):
        """A cut answer that arrives empty is confirmed twice before the walk counts as done."""
        empties = iter([True])
        real = co.Inverter.query_log

        def flaky(inv, name, before):
            if next(empties, False):
                return []  # an empty, cut answer with a valid checksum
            return real(inv, name, before)

        with unittest.mock.patch.object(co.Inverter, "query_log", flaky):
            co.walk_step(self.conn, self.inv, SOC, T0, pause=lambda s: None)
        self.assertEqual(len(self.entries()), 3)
        self.assertIn(SOC, co.walk_pending(self.conn))

    def test_restart_in_the_middle(self):
        """E11: a restart resumes from the oldest stored entry; nothing is stored twice."""
        co.walk_step(self.conn, self.inv, SOC, T0, pause=lambda s: None)
        self.conn.close()
        self.conn = co.connect(self.path)
        while SOC in co.walk_pending(self.conn):
            co.walk_step(self.conn, self.inv, SOC, T0, pause=lambda s: None)
        self.assertEqual(len(self.entries()), 7)

    def catch_up(self):
        cursor = {}
        while not co.catch_up_step(self.conn, self.inv, SOC, T0, cursor, pause=lambda s: None):
            pass

    def test_repeated_read_changes_nothing(self):
        """E11: reading the newest entries again stores nothing twice and changes nothing."""
        self.catch_up()
        first = self.entries()
        self.assertEqual(len(first), 3)
        self.catch_up()
        self.assertEqual(self.entries(), first)

    def test_long_gap_filled(self):
        """E11: a gap longer than one answer (host down for hours) is filled from the present back."""
        with self.conn:
            self.conn.execute("INSERT INTO log_entry VALUES (?, ?, ?, ?)", (SOC, SOC_LOG[0][0], 0.57, "x"))
        self.catch_up()
        self.assertEqual(len(self.entries()), 7)

    def test_nan_answer_does_not_stall_the_walk(self):
        """An answer whose values cannot be stored (NaN) still moves the walk on."""
        nan_log = [(ts, float("nan")) if i < 4 else (ts, v) for i, (ts, v) in enumerate(SOC_LOG)]
        self.fake.logs[SOC] = nan_log
        steps = 0
        while SOC in co.walk_pending(self.conn) and steps < 10:
            co.walk_step(self.conn, self.inv, SOC, T0, pause=lambda s: None)
            steps += 1
        self.assertNotIn(SOC, co.walk_pending(self.conn))
        self.assertEqual(len(self.entries()), 3)

    def test_clock_recorded(self):
        """Log times follow the inverter's clock (UTC+2 on 2026-11-22), so each query stores it."""
        co.walk_step(self.conn, self.inv, SOC, T0, pause=lambda s: None)
        self.assertEqual(self.conn.execute("SELECT read_at, inverter_ts FROM clock").fetchall(),
                         [(co.iso(T0), NOW_TS)])

    def test_only_log_queries_written(self):
        """Requirements 2.3: every write frame sent addresses a logger.* object."""
        co.walk_step(self.conn, self.inv, SOC, T0, pause=lambda s: None)
        writes = [name for command, name in self.fake.commands if command == Command.WRITE]
        self.assertEqual(writes, [SOC])


def diagnostics(delivered="2026-12-20T05:10:00.000Z", yesterday_heating=None, today_heating=None, drop=None):
    """A diagnostics document shaped like daikin_onecta 5.0.4's, with placeholder values only."""
    heating = yesterday_heating or [1, 2, 2, 1, 2, 1, 2, 1, 1, 2, 2, 1]
    today = today_heating or [1] + [None] * 11
    point = lambda kind, modes: {  # noqa: E731
        "embedded_id": "1", "management_point_type": kind,
        "consumption_data": {"value": {"electrical": dict(modes, unit="kWh")}}}
    doc = {"data": {"json_data": [{
        "id": "placeholder", "timestamp": delivered,
        "management_points": [
            {"embedded_id": "gateway", "management_point_type": "gateway"},
            point("climateControl", {"heating": {"day": heating + today, "week": [0] * 14, "month": [0] * 24},
                                     "cooling": {"day": [0] * 12 + [None] * 12, "week": [0] * 14, "month": [0] * 24}}),
            point("domesticHotWaterTank", {"heating": {"day": [0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0] + [None] * 12,
                                                       "week": [0] * 14, "month": [0] * 24}}),
        ]}]}}
    if drop == "day":
        del doc["data"]["json_data"][0]["management_points"][1]["consumption_data"]["value"]["electrical"]["heating"]["day"]
    return doc


TZ = None if REGISTRY is None else co.ZoneInfo("Europe/Berlin")
MORNING = datetime(2026, 12, 20, 5, 30, tzinfo=timezone.utc)  # 06:30 local


@unittest.skipIf(REGISTRY is None, "rctclient is not installed")
class EnergyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = co.connect(str(Path(self.tmp.name) / "c.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def periods(self):
        return self.conn.execute("SELECT day, period_start_local, period_start_utc, management_point, mode, kwh "
                                 "FROM energy_period ORDER BY management_point, mode, period_start_local").fetchall()

    def test_a_day_recorded(self):
        """E12: twelve periods each for space heating (and cooling) and hot water, for yesterday."""
        self.assertTrue(co.energy_step(self.conn, diagnostics, MORNING, TZ))
        rows = self.periods()
        self.assertEqual(len(rows), 36)
        self.assertEqual({r[0] for r in rows}, {"2026-12-19"})
        heating = [r for r in rows if r[3] == "climateControl" and r[4] == "heating"]
        self.assertEqual(sum(r[5] for r in heating), 18)
        self.assertEqual((heating[0][1], heating[0][2]), ("2026-12-19T00:00", "2026-12-18T23:00:00+00:00"))
        self.assertEqual(self.conn.execute("SELECT * FROM failure").fetchall(), [])

    def test_repeated_read_no_duplicate(self):
        calls = []
        fetch = lambda: calls.append(1) or diagnostics()  # noqa: E731
        co.energy_step(self.conn, fetch, MORNING, TZ)
        self.assertTrue(co.energy_step(self.conn, fetch, MORNING, TZ))
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.periods()), 36)

    def test_missing_key_stores_nothing(self):
        """A changed format stores nothing for the day and records a failure (design C7)."""
        self.assertFalse(co.energy_step(self.conn, lambda: diagnostics(drop="day"), MORNING, TZ))
        self.assertEqual(self.periods(), [])
        self.assertEqual([r[1] for r in self.conn.execute("SELECT * FROM failure")], ["energy"])

    def test_not_published_yet_waits(self):
        """A period of yesterday still missing, or data from before midnight: nothing stored, no failure."""
        late = diagnostics(yesterday_heating=[1] * 11 + [None], today_heating=[None] * 12)
        self.assertFalse(co.energy_step(self.conn, lambda: late, MORNING, TZ))
        stale = diagnostics(delivered="2026-12-19T22:50:00.000Z")  # 23:50 local, the day before
        self.assertFalse(co.energy_step(self.conn, lambda: stale, MORNING, TZ))
        self.assertEqual(self.periods(), [])
        self.assertEqual(self.conn.execute("SELECT * FROM failure").fetchall(), [])

    def test_period_without_value_skipped_once_a_later_one_has_one(self):
        """The unit was off 06:00-08:00 local on 2026-11-23: Daikin never filled that period. The
        others are stored, the gap is recorded, and the day is not read again."""
        off = diagnostics(yesterday_heating=[1, 2, 2, None, 2, 2, 1, 3, 1, 3, 2, 2])
        self.assertTrue(co.energy_step(self.conn, lambda: off, MORNING, TZ))
        heating = [r for r in self.periods() if r[3] == "climateControl" and r[4] == "heating"]
        self.assertEqual(len(heating), 11)
        self.assertNotIn("2026-12-19T06:00", [r[1] for r in heating])
        self.assertEqual(len(self.periods()), 35)
        self.assertEqual([r[1:] for r in self.conn.execute("SELECT * FROM failure")],
                         [("energy", "Daikin published no value for 2026-12-19: climateControl heating 06:00")])
        self.assertTrue(co.energy_step(self.conn, lambda: off, MORNING, TZ))
        self.assertEqual(len(self.conn.execute("SELECT * FROM failure").fetchall()), 1)

    def test_last_period_without_value_final_once_today_has_one(self):
        """22:00-24:00 without a value while today's first period has one: final as well."""
        off = diagnostics(yesterday_heating=[1] * 11 + [None])
        self.assertTrue(co.energy_step(self.conn, lambda: off, MORNING, TZ))
        self.assertEqual(len(self.periods()), 35)

    def test_final_gap_waits_for_the_other_series(self):
        """A final gap in heating does not store the day while cooling's last period may still come."""
        doc = diagnostics(yesterday_heating=[1, 2, 2, None, 2, 2, 1, 3, 1, 3, 2, 2])
        doc["data"]["json_data"][0]["management_points"][1]["consumption_data"]["value"]["electrical"][
            "cooling"]["day"] = [0] * 11 + [None] * 13
        self.assertFalse(co.energy_step(self.conn, lambda: doc, MORNING, TZ))
        self.assertEqual(self.periods(), [])
        self.assertEqual(self.conn.execute("SELECT * FROM failure").fetchall(), [])

    def test_whole_day_without_value_waits_quietly(self):
        """Off all of yesterday, on since midnight: nothing to store, so no failure every 15 minutes."""
        doc = diagnostics(yesterday_heating=[None] * 12)
        for kind, mode in (("climateControl", "cooling"), ("domesticHotWaterTank", "heating")):
            point = next(p for p in doc["data"]["json_data"][0]["management_points"]
                         if p["management_point_type"] == kind)
            point["consumption_data"]["value"]["electrical"][mode]["day"] = [None] * 12 + [0] + [None] * 11
        for _ in range(3):
            self.assertFalse(co.energy_step(self.conn, lambda: doc, MORNING, TZ))
        self.assertEqual(self.periods(), [])
        self.assertEqual(self.conn.execute("SELECT * FROM failure").fetchall(), [])

    def test_unexpected_shape_recorded_not_raised(self):
        """Any other shape of the diagnostics is a recorded failure, never a crash (review finding 2)."""
        for doc in (diagnostics(delivered=None), {"data": {"json_data": [{"timestamp": "2026-12-20T05:10:00Z",
                     "management_points": [{"management_point_type": "x",
                                            "consumption_data": {"value": {"electrical": []}}}]}]}}):
            self.assertFalse(co.energy_step(self.conn, lambda: doc, MORNING, TZ))
        self.assertEqual(len(self.conn.execute("SELECT * FROM failure").fetchall()), 2)
        self.assertEqual(self.periods(), [])

    def test_unreachable_home_assistant(self):
        def fetch():
            raise OSError("connection refused")
        self.assertFalse(co.energy_step(self.conn, fetch, MORNING, TZ))
        self.assertEqual([r[1] for r in self.conn.execute("SELECT * FROM failure")], ["energy"])


@unittest.skipIf(REGISTRY is None, "rctclient is not installed")
class WriteGuardTest(unittest.TestCase):
    def test_only_logger_objects(self):
        """Design C6: a write to anything but a logger.* time series is refused before a frame exists."""
        for name in ("power_mng.state", "battery.soc_target", "power_mng.calib_charge_power",
                     "logger.log_rate", "logger.buffer"):
            with self.assertRaises(PermissionError):
                co.log_query_frame(name, 1_800_000_000)

    def test_unanswered_series_is_neither_walked_nor_queried(self):
        """E11: logger.year_log_ts, which this inverter never answered, is left out, so its walk
        does not stay pending and no query frame is built for it."""
        conn = co.connect(":memory:")
        self.assertNotIn("logger.year_log_ts", co.walk_pending(conn))
        self.assertIn("logger.year_eload_log_ts", co.walk_pending(conn))
        with self.assertRaises(PermissionError):
            co.log_query_frame("logger.year_log_ts", 1_800_000_000)

    def test_logger_query_is_a_write_of_a_timestamp(self):
        frame = co.log_query_frame("logger.minutes_soc_log_ts", 1_800_000_000)
        received = ReceiveFrame()
        received.consume(frame)
        self.assertEqual(received.command, Command.WRITE)
        self.assertEqual(REGISTRY.get_by_id(received.id).name, "logger.minutes_soc_log_ts")
        self.assertEqual(int.from_bytes(received.data, "big"), 1_800_000_000)


if __name__ == "__main__":
    unittest.main()
