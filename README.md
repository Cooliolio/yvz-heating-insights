# YVZ Heating Insights

Home Assistant OS add-on that logs a list of Home Assistant states every
5 minutes into monthly CSV files. **Collector only**: no model fitting, no MQTT,
no UI. It is strictly read-only: it only calls `GET /api/states`, never a
service, and only writes inside `heating_data/` in your HA config folder.

Which entities are logged is **your choice**, set in the add-on's Configuration
tab. The list is stored by Home Assistant, not in this repository.

## Install (Home Assistant)

1. **Settings > Add-ons > Add-on Store** > menu (three dots, top right) > **Repositories**.
2. Add `https://github.com/Cooliolio/yvz-heating-insights` and press **Add**.
3. Reload the store page, open **YVZ Heating Insights**, press **Install**.
4. Open the **Configuration** tab, switch to **Edit in YAML** (three dots, top
   right) and paste your column list (format below). Press **Save**.
5. Open the **Info** tab and press **Start**. Leave "Start on boot" on. The
   **Log** tab shows `N columns, output folder /homeassistant/heating_data`.
6. Files appear in the HA config folder (the one that holds `configuration.yaml`)
   under `heating_data/`, one row every 5 minutes at :00, :05, :10 ... Inside the
   add-on this folder is `/homeassistant/heating_data`.

If the options are missing or invalid, the add-on logs the reason, writes
nothing and stops.

## Configuration

```yaml
columns:
  - name: sun_elevation
    entity: sun.sun|elevation
  - name: example_temperature
    entity: sensor.example_temperature
```

- `name`: CSV column name, `a-z`, `0-9`, `_`, starting with a letter. Must be
  unique. `ts_utc` is reserved (it is always the first column).
- `entity`: `domain.object_id`, optionally followed by `|attribute` to log an
  attribute instead of the state.
- Column order in the CSV = order in this list.

### Adding or changing a column later

Edit the list, **Save**, then **Restart** the add-on (options are read at start).
Old files are never rewritten: a changed column list starts a new file
`YYYY-MM_v2.csv` (then `_v3`, ...) with the new header, and `schema_version` is
updated. Changing only the `entity` of an existing column name does *not* start a
new file, because the header only holds names.

## Output

- `YYYY-MM.csv`: one file per UTC month, header on the first line.
- `schema_version`: text file with the current schema number (starts at `1`).

Rules: `ts_utc` is ISO 8601 UTC with `Z`, truncated to the minute. Numbers are
written as-is. `unavailable`, `unknown`, `None` and missing entities are an
**empty cell** (never 0). `binary_sensor` and `switch`: `on` = 1, `off` = 0. Text
states are written as text (CSV-quoted when needed). If the HA API fails, the row
is skipped and the next tick tries again.

## Development

```
cd yvz_heating_insights
python3 -m venv .venv && .venv/bin/pip install requests
.venv/bin/python -m unittest discover -s tests -v

# dry-run against a fake HA with a fake options file
.venv/bin/python tests/fake_ha.py 8123 &
HA_API_URL=http://127.0.0.1:8123/api SUPERVISOR_TOKEN=x \
  .venv/bin/python app/collector.py --dry-run --once --options tests/options.fake.json
```

Options: `--options` (env `OPTIONS_PATH`, default `/data/options.json`),
`--data-dir` (env `HEATING_DATA_DIR`), `--api-url` (env `HA_API_URL`),
`--interval`, `--dry-run`, `--once`. `app/nightly.py` is a stub extension point.
