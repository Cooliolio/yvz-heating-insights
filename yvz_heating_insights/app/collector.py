"""YVZ Heating Insights collector.

Every 5 minutes (wall-clock aligned) read a fixed set of entity states from
Home Assistant and append one row to a monthly CSV file. Read-only: it only
issues GET /api/states and only writes inside the data directory
(/homeassistant/heating_data in the add-on).
"""
import argparse
import csv
import io
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("collector")

INTERVAL_S = 300
MISSING_STATES = {"unavailable", "unknown", "none", ""}
ON_OFF_DOMAINS = {"binary_sensor", "switch"}
FILE_RE = re.compile(r"^(\d{4}-\d{2})(?:_v(\d+))?\.csv$")

DEFAULT_DATA_DIR = "/homeassistant/heating_data"
DEFAULT_API_URL = "http://supervisor/core/api"
DEFAULT_OPTIONS = "/data/options.json"
RESERVED_COLUMNS = {"ts_utc"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+(\|[A-Za-z0-9_]+)?$")

stop_event = threading.Event()


# --- configuration ----------------------------------------------------------

class ConfigError(Exception):
    """The add-on options are missing or invalid."""


def parse_columns(options):
    """Validate the options dict, return [(column, entity_id, attribute_or_None), ...].

    Errors mention the column position and name, never the entity id.
    """
    raw = options.get("columns") if isinstance(options, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ConfigError("options need a non-empty 'columns' list")
    columns, seen = [], set()
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ConfigError("column #%d: expected name and entity" % i)
        name, spec = item.get("name"), item.get("entity")
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise ConfigError("column #%d: invalid name (use a-z, 0-9, _; start with a letter)" % i)
        if name in RESERVED_COLUMNS:
            raise ConfigError("column #%d: name '%s' is reserved" % (i, name))
        if name in seen:
            raise ConfigError("column #%d: duplicate name '%s'" % (i, name))
        if not isinstance(spec, str) or not ENTITY_RE.match(spec.strip()):
            raise ConfigError("column #%d ('%s'): entity must be domain.object_id "
                              "with an optional |attribute" % (i, name))
        seen.add(name)
        entity, _, attr = spec.strip().partition("|")
        columns.append((name, entity, attr or None))
    return columns


def load_columns(path):
    try:
        with open(path, encoding="utf-8") as f:
            options = json.load(f)
    except OSError as exc:
        raise ConfigError("cannot read options file %s: %s" % (path, exc.strerror or exc))
    except ValueError as exc:
        raise ConfigError("options file %s is not valid JSON: %s" % (path, exc))
    return parse_columns(options)


def header_for(columns):
    return ["ts_utc"] + [c[0] for c in columns]


# --- value conversion ---------------------------------------------------------

def _scalar(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        return "" if math.isnan(value) or math.isinf(value) else repr(value)
    if isinstance(value, (int, str)):
        return str(value)
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def convert_value(entity_id, attribute, state_obj):
    """Convert one HA state object to a CSV cell string ('' = missing)."""
    if not state_obj:
        return ""
    if attribute:
        raw = (state_obj.get("attributes") or {}).get(attribute)
        if raw is None:
            return ""
        cell = _scalar(raw)
        if cell.strip().lower() in MISSING_STATES:
            return ""
        return cell
    raw = state_obj.get("state")
    if raw is None:
        return ""
    text = str(raw)
    low = text.strip().lower()
    if low in MISSING_STATES:
        return ""
    if entity_id.split(".", 1)[0] in ON_OFF_DOMAINS:
        if low == "on":
            return "1"
        if low == "off":
            return "0"
    return text


def build_row(columns, states_by_id, ts):
    row = [ts.strftime("%Y-%m-%dT%H:%MZ")]
    for _name, entity, attr in columns:
        row.append(convert_value(entity, attr, states_by_id.get(entity)))
    return row


def format_line(row):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(row)
    return buf.getvalue()


# --- time ---------------------------------------------------------------------

def next_tick(now_ts, interval=INTERVAL_S):
    """Next wall-clock aligned tick strictly after now_ts (epoch seconds)."""
    return (math.floor(now_ts / interval) + 1) * interval


def tick_datetime(tick_ts):
    """UTC datetime of a tick, truncated to the minute."""
    return datetime.fromtimestamp(tick_ts, tz=timezone.utc).replace(second=0, microsecond=0)


# --- storage ------------------------------------------------------------------

def file_name(month, version):
    return "%s.csv" % month if version == 1 else "%s_v%d.csv" % (month, version)


class Store:
    def __init__(self, data_dir):
        self.dir = data_dir
        self.version_file = os.path.join(data_dir, "schema_version")

    def ensure_dir(self):
        os.makedirs(self.dir, exist_ok=True)

    def _files(self):
        """[(month, version, path)] sorted oldest to newest."""
        found = []
        for name in os.listdir(self.dir) if os.path.isdir(self.dir) else []:
            m = FILE_RE.match(name)
            if m:
                found.append((m.group(1), int(m.group(2) or 1), os.path.join(self.dir, name)))
        return sorted(found)

    def schema_version(self):
        try:
            with open(self.version_file, encoding="utf-8") as f:
                return max(1, int(f.read().strip()))
        except (OSError, ValueError):
            return 1

    def _write_schema_version(self, version):
        tmp = self.version_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("%d\n" % version)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.version_file)

    @staticmethod
    def _read_header(path):
        try:
            with open(path, encoding="utf-8", newline="") as f:
                return next(csv.reader(f), None)
        except OSError:
            return None

    def path_for(self, month, header):
        """Pick (and decide the name of) the file to append to. Never rewrites old files."""
        files = self._files()
        this_month = [f for f in files if f[0] == month]
        schema = self.schema_version()
        if this_month:
            _m, version, path = this_month[-1]
            if self._read_header(path) == header:
                return path
            new_version = max(version, schema) + 1
            log.warning("columns changed vs %s; starting new file %s (schema_version -> %d)",
                        os.path.basename(path), file_name(month, new_version), new_version)
        elif files and self._read_header(files[-1][2]) != header:
            new_version = schema + 1
            log.warning("columns changed vs %s; starting %s (schema_version -> %d)",
                        os.path.basename(files[-1][2]), file_name(month, new_version), new_version)
        else:
            new_version = schema
        path = os.path.join(self.dir, file_name(month, new_version))
        if new_version != schema or not os.path.exists(self.version_file):
            self._write_schema_version(new_version)
        return path

    def append(self, ts, header, line):
        """Append one CSV line (header first if the file is new), fsync'd."""
        path = self.path_for(ts.strftime("%Y-%m"), header)
        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "ab+") as f:
            prefix = ""
            if new_file:
                prefix = format_line(header)
            else:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":  # earlier crash left a partial line
                    prefix = "\n"
            f.seek(0, os.SEEK_END)
            f.write((prefix + line).encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        return path


# --- Home Assistant -----------------------------------------------------------

def fetch_states(api_url, token, timeout=20):
    """GET /states once. Returns {entity_id: state_obj}; raises on any failure."""
    resp = requests.get(api_url.rstrip("/") + "/states",
                        headers={"Authorization": "Bearer %s" % token}, timeout=timeout)
    resp.raise_for_status()
    return {s["entity_id"]: s for s in resp.json()}


def collect_once(columns, store, tick_ts, api_url, token, dry_run=False):
    """One tick. Returns the CSV line written/printed, or None if the row was skipped."""
    try:
        states = fetch_states(api_url, token)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        log.error("HA API failed, skipping row: %s", exc)
        return None
    ts = tick_datetime(tick_ts)
    header = header_for(columns)
    line = format_line(build_row(columns, states, ts))
    if dry_run:
        print(format_line(header), end="")
        print(line, end="")
        return line
    try:
        store.append(ts, header, line)
    except OSError as exc:
        log.error("write failed, skipping row: %s", exc)
        return None
    return line


# --- main loop ----------------------------------------------------------------

def _handle_signal(signum, _frame):
    log.info("signal %s received, stopping", signum)
    stop_event.set()


def run(args):
    try:
        columns = load_columns(args.options)
    except ConfigError as exc:
        log.error("%s", exc)
        return 1
    data_dir = args.data_dir
    store = Store(data_dir)
    if not args.dry_run:
        store.ensure_dir()
    log.info("%d columns, output folder %s%s", len(columns) + 1, data_dir,
             " (dry-run)" if args.dry_run else "")
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        log.warning("SUPERVISOR_TOKEN is not set; API calls will fail")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    while not stop_event.is_set():
        tick = next_tick(time.time(), args.interval) if not args.once else \
            math.floor(time.time() / args.interval) * args.interval
        if not args.once and stop_event.wait(max(0, tick - time.time())):
            break
        try:
            collect_once(columns, store, tick, args.api_url, token, args.dry_run)
        except Exception:  # the loop must never die
            log.exception("unexpected error, skipping row")
        if args.once:
            break
    log.info("stopped")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="print header and row instead of writing")
    p.add_argument("--once", action="store_true", help="collect one row now, then exit")
    p.add_argument("--options", default=os.environ.get("OPTIONS_PATH", DEFAULT_OPTIONS),
                   help="add-on options JSON written by the Supervisor")
    p.add_argument("--data-dir", default=os.environ.get("HEATING_DATA_DIR", DEFAULT_DATA_DIR))
    p.add_argument("--api-url", default=os.environ.get("HA_API_URL", DEFAULT_API_URL))
    p.add_argument("--interval", type=int, default=INTERVAL_S)
    return p.parse_args(argv)


def main(argv=None):
    return run(parse_args(argv))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
