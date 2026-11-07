# Standalone component workflows

These commands remain supported for component development and source analysis.
For integrated annual scenarios, start with the repository README. Commands run
from the repository root.

# energy_management

Explore household solar generation, electricity use, a heat pump, and car charging.
Car energy-budget scenarios: `pixi run car --config configs/car.example.toml --output outputs/car_example`.
See [car model assumptions and data needs](car_model.md).
For annual evening charging tables and heatmaps:
`pixi run car-schedule --config configs/car_schedule.example.toml --year 2026 --output outputs/car_schedule_example`.
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
See [equations and limitations](solar_model.md).

## Heat-pump scenarios

The heat-pump component models space heating from hourly KNMI weather. It is
**assumption-driven and uncalibrated**: all physical/control parameters live in
TOML, and optional controller totals are comparisons only. No boiler, tap-water
heating or cooling is included. See [model, configuration and limitations](heat_pump_model.md).

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
handling and recovery debt. See [specifications, equations and limitations](boiler_model.md).
The household CV installation can add heat downstream using gas; that top-up is
outside this model's electrical-consumption boundary. `daily_profile.png` shows
the first simulated day's illustrative electrical demand.

## Regular household electricity

Five configurable annual budgets represent background, lighting, routine activity,
evening-shiftable and weekend-shiftable consumption. Shares are assumptions;
seasonal/hourly shapes can be compared with multiple external households.

```sh
pixi run regular-usage-reference --config configs/regular_usage.example.toml --cache data/regular_usage_source --output outputs/regular_usage_reference
pixi run regular-usage --config configs/regular_usage.example.toml --year 2026 --reference outputs/regular_usage_reference --calibration outputs/regular_usage_reference/calibration.json --output outputs/regular_usage_2026
```

The first command downloads/validates Liander and screened Fluvius profiles,
reports separate household spreads, and fits overall shape on training households.
Add `--offline` to reuse cached reference downloads. The second writes expected
baseline/shifted profiles, 30 seeded pairs, daily/hourly heatmaps, component plots,
and external comparisons. The default annual total is 2,500 kWh; no energy is moved.

Explore moving both assumed flexible budgets to configured destinations:

```sh
pixi run regular-usage --config configs/regular_usage.example.toml --year 2026 --reference outputs/regular_usage_reference --calibration outputs/regular_usage_reference/calibration.json --evening-fraction-moved 1 --weekend-fraction-moved 1 --output outputs/regular_usage_shifted_2026
```

Edit a local copy of the config to change component shares and destination weights.
Omit `--calibration` to control profile shapes directly; `--reference` alone only
adds comparisons. Legacy bias configs are rejected with migration guidance.
External shape agreement does not establish household flexibility or bill savings.
See [model, assumptions, source handling and outputs](regular_usage_model.md)
and [recorded external comparison results](regular_usage_reference_results.md).

## Common component table format

Solar, space heating (each scenario folder), the boiler, and regular electricity write the same
integration tables. See [the shared data contract](energy_tables.md).

| File | Columns |
| --- | --- |
| `hourly_energy.txt` | `interval_start_utc`, `interval_start_local`, `energy_kwh` |
| `daily_energy.txt` | `date`, `energy_kwh` |

Each hourly row is **one elapsed hour**, labeled at its start, with positive kWh
produced (solar) or consumed (heat pump/boiler/regular usage). UTC labels include `+00:00`;
local labels include their actual UTC offset. Join components on UTC, and keep
production and consumption separate. Local DST days have 23/25 hourly rows.
Component-specific files remain available for existing plotting workflows.
Boiler half-hour calculations are summed into hours; hourly diagnostics are saved
in `diagnostics.txt`, with hourly electrical use also in `hourly_consumption.txt`.

## Synthetic weather for planning

Generate fictional hourly weather years by sampling seasonally matched multi-day
blocks from **2015–2025 KNMI observations (11 calendar years)**:

```sh
pixi run weather-scenarios --config configs/weather_scenarios.example.toml --output outputs/weather_scenarios_2027_seed42
```

The example produces 30 seeded samples with temperature, humidity, wind and sunshine
kept together. It assumes a stationary reference climate, without forecasting
specific dates or projecting climate change. Seeds, block length, seasonal window,
join tolerance and target calendar are configurable. Each sample can drive the
heat-pump model; match the model config's target calendar to the generated weather.
See [sampling method, checks and exact consumption commands](weather_scenarios.md).
