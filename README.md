# energy_management

Explore household solar generation, electricity use, a heat pump, and car charging.
The current component generates reproducible solar scenarios independently of a dashboard.

## Setup

```sh
pixi install --locked
pixi run test
```

## Compare the two solar methods

The only retained source input is `data/hourly_yield_2023_2025.txt`, the unchanged
private hourly export. `data/` and `outputs/` are Git-ignored. Old outputs and
astronomical input exports have been removed. The current examples use year 2025,
seed 42, the same measured monthly budgets, and the same measured 3.85 kW
provisional power ceiling:

| Method | Output directory |
| --- | --- |
| Empirical historical daily distributions | `outputs/solar_historical_2025_seed42/` |
| Beta distributions from derived summary statistics | `outputs/solar_statistics_2025_seed42/` |

Each directory contains `daily_heatmap.png` and `january_july_curves.png`, generated
by the same plotting functions with the same color/power scales. Both are smooth
scenarios, not actual 2025 weather reconstructions. No yearly mean is required.

### Historical method

```sh
pixi run solar-history --yields data/hourly_yield_2023_2025.txt --locality Leusden --country-code NL --input-timezone Europe/Amsterdam --interval-label start --year 2025 --seed 42 --output outputs/solar_historical_2025_seed42
```

Accepts 2–5 complete calendar years. TSV columns are `date_time` (`YYYY-MM-DD HH:MM`)
and `hourly_yield_kwh` (energy per one-hour interval). This adapter requires regular
naive interval-start labels with 24 entries per labeled day. Missing, duplicate,
negative, nonfinite and partial-year readings fail. Offset-aware exports and real
23/25-row source days require another import adapter; do not invent or remove readings.

The source timezone and label convention remain unconfirmed. This run explicitly
assumes Europe/Amsterdam and interval-start labels; source daily totals use the
supplied calendar dates without resolving source DST ambiguity. Generated timestamps
handle DST explicitly. Source SHA256 and assumptions are recorded in `run.json`.

By default the largest measured hourly kWh supplies a provisional power cap in kW.
Hourly averages can understate instantaneous power. Supply `--max-power-kw` when
the physical inverter ceiling is known. The historical method derives mean monthly
totals, monthly daily maxima and empirical daily distributions automatically.

### Summary-statistics method

The current `input_statistics.toml` was derived from the same original measurements.
It contains monthly mean totals and measured monthly daily maxima, not fictional
values. Reproduce it from the historical run's fitted config:

```sh
pixi run python -c 'import json; from pathlib import Path; c=json.loads(Path("outputs/solar_historical_2025_seed42/run.json").read_text())["config"]; c["beta_concentration"]=2.879; p=Path("outputs/solar_statistics_2025_seed42/input_statistics.toml"); p.parent.mkdir(parents=True,exist_ok=True); p.write_text("\n".join(f"{k} = {repr(v)}" for k,v in c.items() if v is not None)+"\n")'
pixi run solar-statistics --config outputs/solar_statistics_2025_seed42/input_statistics.toml --year 2025 --seed 42 --output outputs/solar_statistics_2025_seed42
```

A new user can instead provide a TOML with `locality`, optional `country_code`,
`monthly_kwh` (12 mean monthly totals, January first), `max_daily_kwh` (12 daily
ceilings) and `max_power_kw`. Annual energy is the sum of monthly totals.
Optional `beta_concentration` and `width` default to 2 and 0.45. The local
statistics example uses a calibrated concentration of 2.879 to reduce excess
contrast; see the calibration explanation in the model notes. The command above
retains that previously fitted value while deriving the other statistics anew. Optional paired
`latitude`/`longitude` bypass locality lookup. `solar` aliases `solar-statistics`.
Monthly means/maxima alone cannot identify variability; the Beta shape is an
assumption. Historical distributions better reproduce observed cloudy-day frequency.

## Shared downstream files and plots

| File | Interface |
| --- | --- |
| `daily_yields.txt` | `date`, `daily_kwh` |
| `hourly_yields.txt` | `interval_start_utc`, `interval_start_local`, `yield_kwh` |
| `monthly_summary.txt` | Targets, generated totals and daily upper bounds |
| `solar_calendar.txt` | Target-year astronomical daylight events |
| `daily_curve_parameters.txt` | Daily energy, events, width and power cap |
| `run.json` | Schema version, method, config, source, seed and assumptions |

All `.txt` tables are TSV. Hourly rows are one elapsed hour, with unique UTC labels
and offset-aware local labels; Netherlands DST days contain 23/25 rows. Daily and
monthly energy are conserved. Seeds vary daily ordering and slightly vary the
within-month distribution; monthly/annual totals stay fixed. Targeting the same
output directory replaces its modeled files.

Plots read the saved outputs, requiring no original measurements or network:

```sh
pixi run solar-plots --input outputs/solar_historical_2025_seed42
pixi run solar-plots --input outputs/solar_statistics_2025_seed42
```

Locality/solar events use [Open-Meteo](https://open-meteo.com/en/docs/geocoding-api)
and [Sunrise-Sunset.org](https://sunrise-sunset.org/). Generation caches public API
responses in `data/solar_cache/` by default; use `--cache-dir` to choose another
location and `--offline` to require cached responses. Current example runs reused
a temporary cache outside the repository; their solar calendars and API provenance
are saved in each output directory. Keep source attributions in downstream displays.
See [equations and limitations](docs/solar_model.md).

## Heat-pump scenarios

The heat-pump component models space heating from hourly KNMI weather. It is
**assumption-driven and uncalibrated**: all physical/control parameters live in
TOML, and optional controller totals are comparisons only. No boiler, tap-water
heating or cooling is included. See [model, configuration and limitations](docs/heat_pump_model.md).

Start with the sanitized example and edit the private copy:

```sh
mkdir -p data
cp configs/heat_pump.example.toml data/heat_pump.toml
pixi run heat-pump-weather --config data/heat_pump.toml --output data/heat_pump_weather
pixi run heat-pump --config data/heat_pump.toml --weather data/heat_pump_weather --output outputs/heat_pump
pixi run heat-pump-plots --input outputs/heat_pump
pixi run test
```

The existing local `data/heat_pump.toml` contains supplied household settings and
explicit provisional assumptions; do not overwrite it when continuing that work.
Optional local controller references can be included with
`--references data/heat_pump_references.toml` on the model command. These files and
all `data/`/`outputs/` content are ignored by Git.

Weather defaults to 2023–2025 at De Bilt. Set `weather.start` and exclusive
`weather.end` for a partial period; the requested weather must be complete.
Use `--offline` on the weather task to require cached raw responses. Modeling and
plotting use saved inputs and need no network. Keep the downloaded weather directory
separate from model output directories.

Outputs contain `low/`, `base/`, and `high/` scenario TSVs, `scenario_summary.txt`,
`run.json`, `daily_heatmap_YEAR.png`, `winter_days.png`, `winter_profiles.png`, and
`scenario_comparison.png`. Heatmaps show base-model daily electrical kWh; winter
plots show gradual modeled hourly demand under observed weather. Sensitivity
scenarios and weather-day spreads are **not confidence intervals**. Insufficient
winter days omit winter plots. No measured hourly accuracy is claimed.

## Hot-water boiler (aroSTOR B200/5)

A separate simple model uses the supplied **60°C** setpoint, full-tank-equivalent
morning use at 08:30 and half-tank-equivalent evening use at 19:00, every day.
COP, cold-water temperature, recovery power and standing costs are explicit
uncalibrated assumptions. It does not modify the space-heating model.

```sh
pixi run boiler --config configs/boiler.example.toml --start 2026-01-01 --end 2027-01-01 --output outputs/boiler_2026
```

Outputs contain hourly/daily electrical kWh and provenance, with explicit DST
handling and recovery debt. See [specifications, equations and limitations](docs/boiler_model.md).
The household CV installation can add heat downstream using gas; that top-up is
outside this model's electrical-consumption boundary. `daily_profile.png` shows
the first simulated day's illustrative electrical demand.

## Common component table format

Solar, space heating (each scenario folder), and the boiler all write the same
integration tables. See [the shared data contract](docs/energy_tables.md).

| File | Columns |
| --- | --- |
| `hourly_energy.txt` | `interval_start_utc`, `interval_start_local`, `energy_kwh` |
| `daily_energy.txt` | `date`, `energy_kwh` |

Each hourly row is **one elapsed hour**, labeled at its start, with positive kWh
produced (solar) or consumed (heat pump/boiler). UTC labels include `+00:00`;
local labels include their actual UTC offset. Join components on UTC, and keep
production and consumption separate. Local DST days have 23/25 hourly rows.
Component-specific files remain available for existing plotting workflows.
Boiler half-hour calculations are summed into hours; detailed steps are saved
in `diagnostics.txt`, with hourly electrical use also in `hourly_consumption.txt`.
