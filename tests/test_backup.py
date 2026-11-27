"""Tests for tools/backup_from_host.sh, against a throwaway data folder, with an ssh and an scp
that run locally (the "host" is a second home folder on this machine).

Source: the principal, 2026-11-27: back up the collected data on demand into a folder that
OneDrive syncs, leaving out what holds credentials and can be recreated.
"""

import hashlib
import os
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "backup_from_host.sh"


def tree_digest(folder: Path) -> dict[str, str]:
    """Checksums of the files, except SQLite's shared-memory index (-shm), which every reader of
    a store in WAL mode updates and which holds no data."""
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file() and not p.name.endswith("-shm")}


class Backup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        # The host: a home folder with the clone's .env and a data folder in use.
        self.home = self.tmp / "hosthome"
        (self.home / "energymanager").mkdir(parents=True)
        self.data = self.tmp / "data"
        (self.home / "energymanager" / ".env").write_text(f'EM_DATA_DIR="{self.data}"\r\n')
        for rel, table in (("collector/collector.sqlite", "state"), ("forecasts/forecasts.sqlite", "fetch"),
                           ("homeassistant/home-assistant_v2.db", "states")):
            path = self.data / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"CREATE TABLE {table} (x)")
            conn.executemany(f"INSERT INTO {table} VALUES (?)", [(i,) for i in range(100)])
            conn.commit()
            self.addCleanup(conn.close)  # kept open: the store is in use while it is backed up
        (self.data / "unit-panel-figures.csv").write_text("resolution,period_start\n")
        (self.data / "deploy.log").write_text("line\n")
        (self.data / "unit").mkdir()
        (self.data / "unit" / "unit-panel-figures.csv").write_text("resolution,period_start\n")
        (self.data / "weather-history").mkdir()
        (self.data / "weather-history" / "history.json").write_text("{}")
        # Left out: credentials and Home Assistant's configuration.
        (self.data / "tls" / "ca").mkdir(parents=True)
        (self.data / "tls" / "ca" / "ca.key").write_text("secret")
        (self.data / "homeassistant" / "secrets.yaml").write_text("secret: x\n")
        (self.data / "homeassistant" / ".storage").mkdir()
        (self.data / "homeassistant" / ".storage" / "auth").write_text("{}")
        self.before = tree_digest(self.data)
        # ssh runs the command here with the host's home; scp copies the path after "host:".
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        ssh = bin_dir / "ssh"
        ssh.write_text(f'#!/bin/sh\nshift\nexport HOME="{self.home}"\nexec "$@"\n')
        scp = bin_dir / "scp"
        scp.write_text('#!/usr/bin/env python3\nimport shutil, sys\n'
                       'args = [a for a in sys.argv[1:] if a != "-q"]\n'
                       'for src in args[:-1]:\n    shutil.copy(src.split(":", 1)[1], args[-1])\n')
        for tool in (ssh, scp):
            tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        self.dest = self.tmp / "OneDrive" / "energymanager-backup"

    def run_backup(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=self.env)

    def test_backs_up_the_data_and_leaves_out_credentials(self):
        result = self.run_backup("user@host", str(self.dest))
        self.assertEqual(result.returncode, 0, result.stderr)
        runs = list(self.dest.iterdir())
        self.assertEqual(len(runs), 1)
        archives = list(runs[0].glob("*.tar.gz"))
        self.assertEqual(len(archives), 1)
        self.assertTrue((runs[0] / (archives[0].name + ".sha256")).exists())
        with tarfile.open(archives[0]) as tar:
            names = {n.split("/", 1)[1] for n in tar.getnames() if "/" in n}
            tar.extractall(self.tmp / "restored", filter="data")
        for kept in ("collector/collector.sqlite", "forecasts/forecasts.sqlite", "homeassistant/home-assistant_v2.db",
                     "unit-panel-figures.csv", "deploy.log", "unit/unit-panel-figures.csv",
                     "weather-history/history.json"):
            self.assertIn(kept, names)
        self.assertFalse([n for n in names if n.startswith("tls") or "secrets" in n or ".storage" in n])
        restored = next((self.tmp / "restored").iterdir())
        rows = sqlite3.connect(restored / "collector" / "collector.sqlite").execute("SELECT count(*) FROM state")
        self.assertEqual(rows.fetchone()[0], 100)  # the WAL's rows are in the snapshot
        self.assertIn("collector/collector.sqlite: ok", result.stdout)
        self.assertIn("Backup checked:", result.stdout)
        self.assertEqual(tree_digest(self.data), self.before)  # the data folder is unchanged
        self.assertEqual(list(self.home.glob("em-backup.*")), [])  # the host's temporary folder is gone

    def test_a_second_run_adds_a_backup_and_deletes_none(self):
        self.assertEqual(self.run_backup("user@host", str(self.dest)).returncode, 0)
        first = next(self.dest.iterdir())
        (first / "marker").write_text("kept")
        os.rename(first, self.dest / "2000-01-01T0000Z")  # as if made earlier
        self.assertEqual(self.run_backup("user@host", str(self.dest)).returncode, 0)
        self.assertEqual(len(list(self.dest.iterdir())), 2)
        self.assertTrue((self.dest / "2000-01-01T0000Z" / "marker").exists())

    def test_missing_env_fails_and_writes_nothing(self):
        (self.home / "energymanager" / ".env").unlink()
        result = self.run_backup("user@host", str(self.dest))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".env is missing", result.stderr)
        self.assertFalse(self.dest.exists())

    def test_usage(self):
        for args in ((), ("user@host",), ("-oProxyCommand=x", str(self.dest))):
            self.assertEqual(self.run_backup(*args).returncode, 2, args)


if __name__ == "__main__":
    unittest.main()
