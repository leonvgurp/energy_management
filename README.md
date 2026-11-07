# Household energy scenarios

This project brings household solar generation, space heating, hot water, plug-in
hybrid charging and general electricity use together to understand when energy is
produced and consumed, how much is imported or exported, and what electricity may
cost with and without the Dutch **salderingsregeling** (annual net metering). It
supports exploring assumptions and alternatives; the example inputs are illustrative,
not a calibrated prediction of this household or a supplier quote.

## How the workflow fits together

1. **Model each component.** Solar distributes monthly generation budgets over
   days and hours; the space-heating heat pump responds to weather and a building
   heat balance; the separate hot-water boiler models draws and recovery; the PHEV
   model schedules meter-side charging; and general use combines background,
   lighting, routine and potentially shiftable demand. Standalone workflows can
   prepare inputs, examine individual models and compare them with available data.
2. **Combine one year.** A single household config supplies all five models and
   shared weather. Each produces electrical kWh for the same elapsed hours. The
   runner joins them by UTC time, sums consumption, and compares it with solar
   production to calculate self-consumption and grid import/export for each hour.
   Solar and heating share the radiation scenario. General use must exclude the
   separately modeled heating, boiler and car loads to avoid double-counting.
3. **Compare many synthetic years.** The ensemble repeats that annual calculation
   with different seeds and settles each year's grid flows under both saldering
   regimes using the same tariffs. It summarizes the resulting bills and energy
   outcomes, and can compare alternative household configs using paired seeds.
   This is a Monte Carlo ensemble, sometimes called a parametric bootstrap; it is
   not a resampling of measured years. A separate weather workflow can bootstrap
   blocks of observed KNMI weather for use as a file input.

The sample spread describes variation under the chosen assumptions, not a forecast
interval covering all uncertainty. In particular, solar, car and general-use energy
budgets remain fixed across seeds; a weather file is reused across seeds. Equipment,
annual budgets, tariffs and climate uncertainty need separate scenarios. There is no
battery model or investment assessment yet. See [ensemble assumptions and
interpretation](docs/ensemble_workflow.md) and the [annual model contract](docs/household_model.md).

## Run the project

From the repository root, using the locked Pixi environment:

```sh
pixi install --locked
pixi run energy
pixi run test
```

The default `energy` command runs 100 seeds from `configs/ensemble.example.toml`,
which points to `configs/household.example.toml`, and writes to `outputs/ensemble/`.
It works offline with the example settings. For one realization or a faster trial:

```sh
pixi run energy-year --config configs/household.example.toml --output outputs/one_year
pixi run energy --config configs/ensemble.example.toml --output outputs/trial --no-plots
```

For a private scenario, copy the examples into the ignored data folder and edit them:

```sh
mkdir -p data/overall
cp configs/household.example.toml data/overall/household.toml
cp configs/ensemble.example.toml data/overall/ensemble.toml
# Set scenario_config = "household.toml" and output = "../../outputs/my_household"
# in data/overall/ensemble.toml.
pixi run energy --config data/overall/ensemble.toml
```

Workflow paths are relative to the workflow config; weather file paths are relative
to the household config. Keep private measurements and household settings in
ignored `data/`; generated results in `outputs/` are also ignored. Rerunning into
the same output directory replaces generated files.

## Explore configurations

The complete example configs contain comments and units. Useful controls include:

| Config section | Choices to explore |
| --- | --- |
| Ensemble `[ensemble]` | Number of runs, first seed, reference calendar, output location and plots. `[alternatives]` names complete household configs to compare with the reference using the same seeds. |
| `[weather]` | Synthetic monthly statistics or a weather file; monthly temperatures and radiation, sunshine factors, global/winter temperature offsets and variability. |
| `[solar]` | Monthly generation targets, daily and power limits, and profile width. Sunshine factors also scale solar targets; infeasible targets fail validation. |
| `[heat_pump]` | Low/base/high scenario, building heat loss and capacity, heating control, operating limits and assumed COP/capacity performance map. |
| `[boiler]` | Tank and water temperatures, COP, recovery power, standing use and hot-water draw schedule. |
| `[phev]` | Annual meter-side energy, charge power and start time, daily charging cycle and seasonality. |
| `[general]` | Annual budget, component shares, seasonal and daily shapes, variability, and fractions and time windows for moving flexible use. Shares must sum to one. |
| `[costs]` | Single/dual meter, import and export rates, off-peak schedule, fixed charges, export fees and annual netting allocation. `saldering` selects the reported single-year regime; both regimes are always calculated for comparison. |
| `[plots]` | Heatmap color scale, winter/summer months and high-consumption threshold. |

The internal baseline disables **general-use shifting only**, keeping the same
weather and random draws. The example moves no general use, so its baseline savings
are zero. To compare another choice, copy the full household config, change the
assumption, and add it under `[alternatives]` in the ensemble config. If tariffs also
change, the comparison includes both tariff and energy effects. See
[configuration and paired comparisons](docs/ensemble_workflow.md#configuration-and-choices).

The default reference calendar is 2001: it supplies dates, weekdays and DST, not
historical 2001 weather or a forecast year. Choose another calendar when needed,
including a leap year:

```sh
pixi run energy-year --calendar-year 2004 --output outputs/leap_calendar
```

## Single-year outputs

The table below describes `pixi run energy-year` (default `outputs/household`).
The main ensemble workflow keeps the mean hourly/daily energy and plot layouts;
its [additional statistical and cost tables](docs/ensemble_workflow.md#tables-and-plots)
retain all seeds and paired differences. Ensemble flags are probabilities.

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

Start with the overall daily heatmaps to see seasonal patterns in production,
consumption, balance and grid flows. Component heatmaps show which loads drive
those patterns. The seasonal day profiles show when solar and individual loads
coincide, while ensemble annual and monthly cost plots compare the two settlement
regimes. Use `annual_summary.txt` for simulated spread and itemized annual bills,
`monthly_summary.txt` for seasonal contributions, and `choice_differences_summary.txt`
for paired alternative savings. Monthly costs are allocations of annual settlement,
not separate monthly invoices. See the [full ensemble output guide](docs/ensemble_workflow.md#tables-and-plots).

Heatmaps share one plotting function and layout, defaulting to `magma`; set
`plots.cmap = "viridis"` to change it. Each labeled colorbar uses that table's
range. Missing calendar dates stay blank.

Default day plots select the best and worst **daily production minus consumption**
in winter (December–February) and summer (June–August), with configurable season
months. Each figure has modeled outdoor temperature and a unitless triangular sun profile above
totals/net balance and consumption by modality. The sun indicator rises linearly
from sunrise to solar noon and falls linearly to sunset; it is not irradiance. Select any local date or regenerate plots from saved data:

```sh
pixi run energy-year --day 01-15 --day 07-15
pixi run energy-plots --input outputs/household --day 10-28
```

Omit `--day` for the four seasonal examples. `--no-plots` generates tables only.
See [the annual model contract and assumptions](docs/household_model.md) for
weather inputs, equations, baseline interpretation and the reusable Python API.

## Code and component workflows

`src/energy_management/components/` contains one model module per modality:
`solar.py`, `heat_pump.py`, `boiler.py`, `phev.py`, `general.py`.
The main repeated-year workflow lives in `ensemble.py`. Single-year orchestration
and configuration live in `household.py`/`config.py`; calendar,
weather, table validation/export and plotting live in `calendar.py`, `weather.py`,
`energy_tables.py`, and `plots.py`. Existing component entry points remain compatible.

[Standalone workflows and source-data preparation](docs/component_workflows.md)
retain the original commands for solar fitting, KNMI weather, weather bootstrap,
car budgets and regular-use reference analysis. Component example configs remain
available for those commands; each single-year simulation reads one household config.
Private sources stay unchanged in ignored `data/`; generated results stay in
ignored `outputs/`. Organize new inputs under `data/<modality>/` or `data/overall/`.

## Tests

`pixi run test` runs the automated suite. Broadly, it checks component calculations
and constraints, input validation, hourly/daily energy accounting and time-zone/DST
boundaries, as well as integrated scenarios, cost settlement and ensemble summaries.
These checks establish code behavior; they do not calibrate the model to this
household or validate future prices. `pixi run smoke` checks scientific imports.

## Electricity costs

For the saved household configuration with **100 historical-weather bootstrap
years**, paired saldering bills and explanatory seasonal figures, see the
[household bootstrap analysis](docs/household_bootstrap_analysis.md).

The `[costs]` section (required for ensembles, optional for single years) includes
the supplied GreenChoice prices and a
`saldering = true` / `false` toggle. Every annual run calculates both regimes with
identical energy flows and tariffs, plus the general-use baseline. Results with
itemized charges are in the ensemble's `annual_runs.txt` / `annual_summary.txt`
tables, or `data/overall/annual_costs.json` for single years. Annual EUR totals also
appear in `run.json` and terminal output. The default is single-rate billing;
set `meter = "dual"` and check offpeak hours/holidays and netting allocation if
applicable. Prices and tax treatment are explicit assumptions, not a 2027 quote.

[Cost model, supplier research, assumptions and configuration](docs/electricity_costs.md)
explain per-kWh export fees, annual bands, fixed charges and the limits for dynamic
contracts. Statutory saldering ends on 1 January 2027; use updated 2027 contract
rates for a forecast, or keep rates constant to isolate just the toggle's effect.
