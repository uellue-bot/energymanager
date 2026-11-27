#!/usr/bin/env bash
# Back up the stack's collected data from the host to a local folder, on demand.
#
# Usage, on the machine that keeps the backups (e.g. a laptop with a OneDrive folder):
#   tools/backup_from_host.sh HOST DEST
#     HOST  ssh destination of the host, e.g. user@host
#     DEST  local folder; each run creates DEST/<UTC time>/ with one archive and its checksum
#
# On the host, it reads the data folder from .env in the host's clone ($EM_BACKUP_CLONE,
# default ~/energymanager), takes consistent snapshots of the SQLite stores while the stack
# runs (SQLite's backup, not a file copy), copies the small data files, and packs them into
# one archive in a temporary folder in the host user's home, which it removes afterwards.
# Locally, it checks the archive's checksum, unpacks it into a temporary folder and runs
# SQLite's integrity check on every store. It changes nothing in the data folder and
# deletes no earlier backups.
#
# Left out on purpose, since they hold credentials and can be recreated (README, "Backup"):
# the certificate authority (tls/), Home Assistant's configuration (.storage/,
# secrets.yaml and the YAML files) and .env.
set -euo pipefail

usage="usage: $0 HOST DEST"
[ "$#" -eq 2 ] && [ -n "$1" ] && [ -n "$2" ] || { echo "$usage" >&2; exit 2; }
host=$1 dest=$2
case $host in -*) echo "HOST must be an ssh destination, not an option: $host" >&2; exit 2 ;; esac
clone=${EM_BACKUP_CLONE:-energymanager}

# On the host: snapshot, pack, print the temporary folder and the archive's name.
remote=$(ssh "$host" bash -s -- "$clone" <<'REMOTE'
set -euo pipefail
cd "$HOME"
cd "$1"
[ -f .env ] || { echo ".env is missing in the host's clone" >&2; exit 1; }
data=$(sed -n 's/^EM_DATA_DIR=//p' .env | tail -n 1 | tr -d '\r')
data=${data#[\"\']}
data=${data%[\"\']}
[ -n "$data" ] && [ -d "$data" ] || { echo "EM_DATA_DIR is not set in .env or does not exist" >&2; exit 1; }
stamp=$(date -u +%Y-%m-%dT%H%MZ)
tmp=$(mktemp -d "$HOME/em-backup.XXXXXX")
name="energymanager-backup-$stamp"
out="$tmp/$name"
mkdir -p "$out"
python3 - "$data" "$out" <<'PY'
import os, sqlite3, sys
data, out = sys.argv[1], sys.argv[2]
for rel in ("collector/collector.sqlite", "forecasts/forecasts.sqlite", "homeassistant/home-assistant_v2.db"):
    path = os.path.join(data, rel)
    if not os.path.exists(path):
        print(f"not found, skipped: {rel}", file=sys.stderr)
        continue
    os.makedirs(os.path.join(out, os.path.dirname(rel)), exist_ok=True)
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    target = sqlite3.connect(os.path.join(out, rel))
    source.backup(target)
    target.close()
    source.close()
PY
for f in "$data"/*.csv "$data"/*.tsv "$data"/deploy.log; do
    [ -f "$f" ] && cp -p "$f" "$out/"
done
for d in unit weather-history; do
    [ -d "$data/$d" ] && cp -pr "$data/$d" "$out/"
done
tar -C "$tmp" -czf "$tmp/$name.tar.gz" "$name"
(cd "$tmp" && sha256sum "$name.tar.gz" > "$name.tar.gz.sha256")
echo "$tmp $name"
REMOTE
)
read -r tmp name <<< "$remote"
case $tmp in
    */em-backup.??????) ;;
    *) echo "unexpected answer from the host: $remote" >&2; exit 1 ;;
esac

cleanup_remote() {
    # Only the temporary folder this run created, named em-backup.XXXXXX.
    ssh "$host" bash -s -- "$tmp" <<'CLEAN'
case $1 in */em-backup.??????) rm -rf -- "${1:?}" ;; esac
CLEAN
}
trap cleanup_remote EXIT

target="$dest/${name#energymanager-backup-}"
mkdir -p "$target"
scp -q "$host:$tmp/$name.tar.gz" "$host:$tmp/$name.tar.gz.sha256" "$target/"
(cd "$target" && sha256sum -c --quiet "$name.tar.gz.sha256")

check=$(mktemp -d)
trap 'rm -rf -- "${check:?}"; cleanup_remote' EXIT
tar -C "$check" -xzf "$target/$name.tar.gz"
python3 - "$check/$name" <<'PY'
import os, sqlite3, sys
root = sys.argv[1]
stores = [os.path.join(d, f) for d, _, files in os.walk(root) for f in files if f.endswith((".sqlite", ".db"))]
for path in sorted(stores):
    result = sqlite3.connect(f"file:{path}?mode=ro", uri=True).execute("PRAGMA integrity_check").fetchone()[0]
    print(f"{os.path.relpath(path, root)}: {result}")
    if result != "ok":
        sys.exit(f"integrity check failed: {path}")
if not stores:
    sys.exit("the archive holds no store")
PY
echo "Backup checked: $target/$name.tar.gz ($(du -h "$target/$name.tar.gz" | cut -f1))"
