# Household energy scenarios

Model a full January–December household energy balance from one TOML config:
solar production, space heating, a separate heat-pump hot-water boiler, PHEV
charging, and general electricity consumption. Calculations use Python/Polars;
plots use Matplotlib/Seaborn. No dashboard or live-device control is involved.

```sh
pixi install --locked
pixi run energy
pixi run test
```

`pixi run energy` uses `configs/household.example.toml` and writes
`outputs/household/`. The example runs offline and contains **illustrative
assumptions, not household calibration**. Every config covers the entire year
and contains the characteristics, budgets, schedules, variability and weather
adjustments for all five modalities.

For a private scenario, copy the example into the ignored data folder:

```sh
mkdir -p data/overall
cp configs/household.example.toml data/overall/household.toml
pixi run energy --config data/overall/household.toml --output outputs/my_household
```

Adjust `weather.sunshine_factor` for a sunnier/darker year;
`weather.winter_temperature_offset_c` for a warmer/colder winter (negative is
colder). Monthly factors and temperature means are configurable too. Solar
production and space-heating solar gains share the radiation scenario. Equipment
limits remain enforced: infeasible solar targets raise an error.

Configs have no target year or partial date window. Output timestamps use **2001
as a reference calendar**, solely to define weekdays, dates and DST; this is not
modeled historical 2001 weather. Override the calendar explicitly when needed,
including leap years:

```sh
pixi run energy --calendar-year 2004 --output outputs/leap_calendar
```

## Outputs

All energy columns are **electrical kWh per elapsed hour**. Positive net balance
means surplus; negative means a grid shortfall. General consumption must exclude
the separately modeled space heating, boiler and car to avoid double-counting.

| Path under the output directory | Contents |
| --- | --- |
| `data/overall/hourly_energy.txt` | UTC/local timestamps; five modalities; total production/consumption; net balance; grid import/export; self-consumption; surplus/high-use flags |
| `data/overall/daily_energy.txt` | Local-calendar daily sums and elapsed hours |
| `data/overall/baseline_hourly_energy.txt` | Same realization with general-use shifting disabled |
| `data/{solar,heat_pump,boiler,phev,general}/` | Common hourly/daily energy tables and modality diagnostics |
| `data/weather/diagnostics.txt` | Shared hourly weather input after adjustments |
| `plots/{solar,heat_pump,boiler,phev,general}/` | Daily energy heatmaps, months in rows and days in columns |
| `plots/overall/` | Total/balance/import/export heatmaps and three-panel daily line plots |
| `scenario.toml`, `run.json` | Config snapshot, hashes, assumptions, totals and selected dates |

Tables are TSV. UTC timestamps uniquely identify hours; local timestamps retain
their offsets. DST days have 23 or 25 intervals. Grid flows use hourly netting;
they do not estimate simultaneous imports/exports inside an hour.

## Plots

Heatmaps share one plotting function and layout, defaulting to `magma`; set
`plots.cmap = "viridis"` to change it. Each labeled colorbar uses that table's
range. Missing calendar dates stay blank.

Default day plots select the best and worst **daily production minus consumption**
in winter (December–February) and summer (June–August), with configurable season
months. Each figure has modeled outdoor temperature and a unitless triangular sun profile above
totals/net balance and consumption by modality. The sun indicator rises linearly
from sunrise to solar noon and falls linearly to sunset; it is not irradiance. Select any local date or regenerate plots from saved data:

```sh
pixi run energy --day 01-15 --day 07-15
pixi run energy-plots --input outputs/household --day 10-28
```

Omit `--day` for the four seasonal examples. `--no-plots` generates tables only.
See [the annual model contract and assumptions](docs/household_model.md) for
weather inputs, equations, baseline interpretation and the reusable Python API.

## Code and component workflows

`src/energy_management/components/` contains one model module per modality:
`solar.py`, `heat_pump.py`, `boiler.py`, `phev.py`, `general.py`.
Shared orchestration/configuration live in `household.py`/`config.py`; calendar,
weather, table validation/export and plotting live in `calendar.py`, `weather.py`,
`energy_tables.py`, and `plots.py`. Existing component entry points remain compatible.

[Standalone workflows and source-data preparation](docs/component_workflows.md)
retain the original commands for solar fitting, KNMI weather, weather bootstrap,
car budgets and regular-use reference analysis. Component example configs remain
available for those commands; the annual command reads only one household config.
Private sources stay unchanged in ignored `data/`; generated results stay in
ignored `outputs/`. Organize new inputs under `data/<modality>/` or `data/overall/`.

## Electricity costs

The optional `[costs]` section includes the supplied GreenChoice prices and a
`saldering = true` / `false` toggle. Every annual run calculates both regimes with
identical energy flows and tariffs, plus the general-use baseline. Results with
itemized charges are in `data/overall/annual_costs.json`; annual EUR totals also
appear in `run.json` and terminal output. The default is single-rate billing;
set `meter = "dual"` and check offpeak hours/holidays and netting allocation if
applicable. Prices and tax treatment are explicit assumptions, not a 2027 quote.

[Cost model, supplier research, assumptions and configuration](docs/electricity_costs.md)
explain per-kWh export fees, annual bands, fixed charges and the limits for dynamic
contracts. Statutory saldering ends on 1 January 2027; use updated 2027 contract
rates for a forecast, or keep rates constant to isolate just the toggle's effect.
