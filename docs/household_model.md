# Integrated annual model

The main CLI is now `pixi run energy`, the [repeated-seed ensemble workflow](ensemble_workflow.md).
The single-year CLI described below is `pixi run energy-year`; its reusable model
and detailed household configuration remain the foundation of each realization.

## Calendar and configuration

`configs/household.example.toml` is the complete public input contract. It includes
all model inputs inline, without loading component configs. It describes a full
January–December scenario, independent of a specific historical or forecast year.
The runner uses reference year 2001 by default (365 days); `--calendar-year`
changes only its calendar, weekdays and DST. Explicitly selecting a leap calendar
adds February 29. Annual budget models retain their annual budgets; calendar-based
weather and heating may consequently have different totals.

The timezone and run seed are shared. General consumption uses one stochastic
realization from `scenario.seed`; its `variability` sigmas/correlation determine
the shape. Standalone ensemble sample/seed settings are omitted from this config;
the annual runner uses one realization and the shared scenario seed.
The heat-pump `scenario` selects the existing low/base/high assumptions. Compare
separate saved configs to explore weather/equipment sensitivities; those scenarios
are assumptions, not confidence intervals or guaranteed savings.

## Weather and solar coupling

The offline example's `monthly_statistics` weather is deliberately illustrative:

- Monthly temperature means are interpolated cyclically through mid-month.
  A configurable afternoon-peaking cosine, stationary AR(1) hourly temperature
  noise, a global offset, and a winter-only offset are added (all in °C).
- Daily cloud weights follow `exp(AR1 - sigma²/2)`. The AR process has stationary
  variance `sigma²`, with innovation deviation `sigma * sqrt(1-rho²)`.
- A daylight sine profile multiplied by cloud weights is normalized to the
  configured monthly irradiation (kWh/m²). Humidity and wind are constant assumed
  inputs in this simplified mode.
- The existing approximate solar-elevation function supplies five-minute daylight
  bounds; noon is their midpoint. This is an approximation without a network lookup,
  shading, atmospheric refraction, or panel-orientation modeling. Locations without
  sunrise/sunset every day or DST changes during daylight are rejected.
- `sunshine_factor * monthly_sunshine_factors[month]` scales both radiation and
  the configured solar monthly kWh targets. Solar's existing bounded allocator uses
  daily radiation as weights, preserving scaled monthly budgets under daily/power
  ceilings. Its Gaussian hourly integration conserves each daily energy budget.
  Impossible budgets fail instead of silently clipping away energy.

The daily solar ordering and space-heating gains share weather. Intraday PV remains
a smooth Gaussian; temperature/cloud noise are independent. This is not a calibrated
joint climate model, and does not support inferred probabilities of savings or
extremes. Solar `beta_concentration` remains available to standalone solar sampling;
the integrated daily-weather weighting replaces Beta sampling.

For observed or seasonally bootstrapped weather, set `weather.mode = "file"` and
`weather.file` to a TSV path **relative to the household config**. The existing
`weather.txt` schema needs `interval_start_utc`, `outdoor_c`, `humidity_pct`,
`wind_m_s`, `radiation_kwh_m2`, with exact full reference-calendar coverage. Offset,
continuity, uniqueness, finite values and physical humidity/wind/radiation bounds
are validated. Local labels are reconstructed from UTC; no date remapping,
interpolation, gap filling or forecast claim is made. Select `--calendar-year`
matching an existing source member, or generate a bootstrap member for the desired
reference calendar using the [standalone workflow](component_workflows.md).
Source bytes are preserved and hashed. Weather offsets and sunshine factors apply
to file inputs too; avoid applying the same adjustment twice. Solar budgets remain
explicit config targets in file mode; source radiation sets their daily distribution.

## Modalities and boundaries

All outputs represent electrical energy, not heat output or EUR costs:

| Module under `components/` | Model retained by the annual runner |
| --- | --- |
| `solar.py` | Monthly energy targets, bounded daily allocation, capped daylight Gaussian integration |
| `heat_pump.py` | Building heat balance, control lag, emitter/capacity limits and interpolated COP; space heating only |
| `boiler.py` | Hot-water-equivalent draw recovery queue, heat-pump/resistance heating and standing allowance |
| `phev.py` | Meter-side charge budgets, shuffled cycle, seasonality, charging power and start time |
| `general.py` | Five annual sub-budgets, seasonal activity/lighting, correlated variability, energy-conserving shifts |

See the existing [solar](solar_model.md), [heat-pump](heat_pump_model.md),
[boiler](boiler_model.md), [car](car_model.md) and [general-use](regular_usage_model.md)
notes for modality equations. The annual runner never applies car charging losses
again: PHEV budgets are already meter-side kWh. General-use annual totals must exclude
the other modeled loads. The hot-water boiler is independent of the space-heating
unit; heat extracted by a boiler from indoor air is not fed back into the building
model. Boiler COP and car seasonality are explicit assumptions, without a new
implicit temperature dependence.

Building temperature starts from the configured initial condition. Boiler recovery
starts empty and may finish with outstanding thermal debt; diagnostics retain it.
No cyclic-year equilibrium or warm-up is claimed. Diagnostic tables retain heating
shortfalls, performance-map boundary flags, indoor temperatures and boiler debt.

## Shared accounting

Each component is validated by `energy_tables.hourly_energy` and must cover exactly
the common elapsed-hour calendar. Half-hour boiler energy is **summed**, not averaged.
No missing modality energy becomes zero. All component energy must be finite and
nonnegative, with UTC and offset-aware local labels agreeing with the configured zone.

For each hour, with production `P` and consumption `C`:

```text
P = solar
C = space_heating + boiler + PHEV + general
net_balance = P - C
import = max(C - P, 0)
export = max(P - C, 0)
self_consumption = min(P, C)
P + import = C + export
```

`overproduction` marks `net_balance > 0`. `high_consumption` marks hours at or above
the annual consumption quantile specified in `plots.high_consumption_quantile`;
its numeric threshold and annual peak appear in `run.json`. Daily tables sum by
local date, including all 23/25 DST hours. Energy does not become power except as
the hourly average (kWh in one elapsed hour equals average kW for that interval).
The optional cost model bills the resulting grid flows. There are no battery flows
or subhourly netting in this integration.

The baseline uses identical weather, equipment and random draws, but disables
**general-use movement only**. Compare `baseline_hourly_energy.txt` against
`hourly_energy.txt` to assess those shifts; this is not a no-heat-pump or alternate
weather baseline. With default zero movement the two files are identical.

## Layout and reusable API

```text
src/energy_management/
  components/{solar,heat_pump,boiler,phev,general}.py
  calendar.py       shared elapsed-hour calendar / solar elevation proxy
  config.py         one household config and validation
  weather.py        shared weather and approximate daylight events
  energy_tables.py  common interval validation and exports
  household.py      simulation, strict integration, summaries, CLI
  plots.py          common calendar heatmap and three-panel day plot
```

Older root module names are compatibility entry points to the canonical component
implementations. Specialized standalone source-analysis/diagnostic plots remain
available; integrated scenario plots all use `plots.py`.

```python
from energy_management.config import load_config
from energy_management.household import simulate
from energy_management.plots import calendar_heatmap, day_profile, select_example_days

config = load_config("configs/household.example.toml")
hourly, components, diagnostics, baseline = simulate(config)
figure = day_profile(hourly, "01-15", weather=diagnostics["weather"],
                     solar_events=diagnostics["solar"])
examples = select_example_days(hourly)
```

Call `simulate(config, config_dir=...)` with the config's parent directory for
relative weather-file inputs. `run(config_path, output)` resolves that automatically
and saves data, plots, config/source/implementation SHA256 hashes and assumptions.
Rerunning replaces the generated files for that scenario; distinct configs should
use distinct output directories. Private source paths and `run.json` provenance
remain in ignored output folders.
