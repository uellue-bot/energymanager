#!/usr/bin/env bash
# Back up the stack's collected data from the host to a local folder, on demand.
#
# Usage, on the machine that keeps the backups (e.g. a laptop with a OneDrive folder):
#   tools/backup_from_host.sh HOST DEST
#     HOST  ssh destination of the host, e.g. user@host
#     DEST  local folder; each run creates DEST/<UTC time>/ with one archive and its checksum
#
# On the host, it reads the data folder from .env in the host's clone ($EM_BACKUP_CLONE,
# default ~/energymanager, relative to the host user's home), takes consistent snapshots of
# the SQLite stores while the stack runs (SQLite's backup, not a file copy), copies the
# small data files, and packs them into one archive in a temporary folder in the host
# user's home, which it removes afterwards, also on failure. It first checks that the home
# has room for twice the stores' size. Locally, the archive goes to DEST/<UTC time>.partial/
# and is renamed to DEST/<UTC time>/ only after its checksum matches and SQLite's integrity
# check passes for every store; a run that fails leaves a .partial folder or nothing. It
# changes nothing in the data folder and deletes no earlier backups.
#
# Left out on purpose, since they hold credentials and can be recreated (README, "Backup"):
# the certificate authority (tls/), Home Assistant's configuration (.storage/,
# secrets.yaml and the YAML files) and .env.
set -euo pipefail

usage="usage: $0 HOST DEST"
[ "$#" -eq 2 ] && [ -n "$1" ] && [ -n "$2" ] || { echo "$usage" >&2; exit 2; }
host=$1 dest=$2
case $host in -*) echo "HOST must be an ssh destination, not an option: $host" >&2; exit 2 ;; esac
# An absolute path, so that scp never reads "name:..." in DEST as a remote host.
case $dest in /*) ;; *) dest="$PWD/$dest" ;; esac
clone=${EM_BACKUP_CLONE:-energymanager}

# Arguments reach the host's shell once more; %q quotes them for it.
remote=$(ssh "$host" "bash -s -- $(printf '%q' "$clone")" <<'REMOTE'
set -euo pipefail
cd "$HOME"
cd "$1"
[ -f .env ] || { echo ".env is missing in the host's clone" >&2; exit 1; }
data=$(sed -n 's/^EM_DATA_DIR=//p' .env | tail -n 1 | tr -d '\r')
data=${data#[\"\']}
data=${data%[\"\']}
[ -n "$data" ] && [ -d "$data" ] || { echo "EM_DATA_DIR is not set in .env or does not exist" >&2; exit 1; }
stores="collector/collector.sqlite forecasts/forecasts.sqlite homeassistant/home-assistant_v2.db"
size=0
for s in $stores; do
    if [ ! -f "$data/$s" ]; then echo "store not found: $data/$s" >&2; exit 1; fi
    size=$((size + $(stat -c %s "$data/$s")))
    if [ -f "$data/$s-wal" ]; then size=$((size + $(stat -c %s "$data/$s-wal"))); fi
done
free=$(df -P -B1 "$HOME" | awk 'NR == 2 {print $4}')
[ "$free" -gt $((2 * size + 100000000)) ] || {
    echo "not enough room in $HOME: $free bytes free, $((2 * size + 100000000)) needed" >&2; exit 1; }
stamp=$(date -u +%Y-%m-%dT%H%M%SZ)
tmp=$(mktemp -d "$HOME/em-backup.XXXXXX")
done_ok=0
trap '[ "$done_ok" = 1 ] || rm -rf -- "${tmp:?}"' EXIT
name="energymanager-backup-$stamp"
out="$tmp/$name"
mkdir -p "$out"
python3 - "$data" "$out" $stores <<'PY'
import os, sqlite3, sys
data, out, stores = sys.argv[1], sys.argv[2], sys.argv[3:]
for rel in stores:
    path = os.path.join(data, rel)
    wal, shm = os.path.exists(path + "-wal"), os.path.exists(path + "-shm")
    if wal and shm:  # in use: a read-only reader, consistent while the service writes
        uri = f"file:{path}?mode=ro"
    elif not wal:  # not in use: nothing to read from a log, and nothing may be created here
        uri = f"file:{path}?mode=ro&immutable=1"
    else:  # opening it would create a -shm owned by this user, which the service may not write
        sys.exit(f"{rel}: a -wal without -shm; back it up while the stack runs or after a clean stop")
    os.makedirs(os.path.join(out, os.path.dirname(rel)), exist_ok=True)
    source = sqlite3.connect(uri, uri=True, timeout=30)
    target = sqlite3.connect(os.path.join(out, rel))
    source.backup(target)
    target.close()
    source.close()
PY
for f in "$data"/*.csv "$data"/*.tsv "$data"/deploy.log; do
    if [ -f "$f" ]; then cp -p "$f" "$out/"; fi
done
for d in unit weather-history; do
    if [ -d "$data/$d" ]; then cp -pr "$data/$d" "$out/"; fi
done
tar -C "$tmp" -czf "$tmp/$name.tar.gz" "$name"
rm -rf -- "${out:?}"
(cd "$tmp" && sha256sum "$name.tar.gz" > "$name.tar.gz.sha256")
done_ok=1
printf '%s\n%s\n' "$tmp" "$name"
REMOTE
)
tmp=$(printf '%s\n' "$remote" | sed -n 1p)
name=$(printf '%s\n' "$remote" | sed -n 2p)
case $tmp in
    */em-backup.??????) ;;
    *) echo "unexpected answer from the host: $remote" >&2; exit 1 ;;
esac
case $name in
    energymanager-backup-????-??-??T??????Z) ;;
    *) echo "unexpected answer from the host: $remote" >&2; exit 1 ;;
esac

check=""
cleanup() {
    [ -z "$check" ] || rm -rf -- "${check:?}"
    # Only the temporary folder this run created on the host, named em-backup.XXXXXX.
    ssh "$host" "bash -s -- $(printf '%q' "$tmp")" <<'CLEAN'
case $1 in */em-backup.??????) rm -rf -- "${1:?}" ;; esac
CLEAN
}
trap cleanup EXIT

stamp=${name#energymanager-backup-}
partial="$dest/$stamp.partial"
final="$dest/$stamp"
[ ! -e "$final" ] && [ ! -e "$partial" ] || { echo "already exists: $final" >&2; exit 1; }
mkdir -p "$partial"
scp -q "$host:$tmp/$name.tar.gz" "$host:$tmp/$name.tar.gz.sha256" "$partial/"
(cd "$partial" && sha256sum -c --quiet "$name.tar.gz.sha256")

check=$(mktemp -d)
tar -C "$check" -xzf "$partial/$name.tar.gz"
python3 - "$check/$name" <<'PY'
import os, sqlite3, sys
root = sys.argv[1]
expected = ["collector/collector.sqlite", "forecasts/forecasts.sqlite", "homeassistant/home-assistant_v2.db"]
for rel in expected:
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        sys.exit(f"missing in the archive: {rel}")
    result = sqlite3.connect(f"file:{path}?mode=ro", uri=True).execute("PRAGMA integrity_check").fetchone()[0]
    print(f"{rel}: {result}")
    if result != "ok":
        sys.exit(f"integrity check failed: {rel}")
PY
mv -- "$partial" "$final"
echo "Backup checked: $final/$name.tar.gz ($(du -h "$final/$name.tar.gz" | cut -f1))"
