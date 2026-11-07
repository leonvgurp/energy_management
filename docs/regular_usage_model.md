# Regular electricity: seasonal demand and movable consumption

This component models regular household electricity only. It excludes car charging,
space heating and hot-water heat pumps. The provisional annual budget is 2,500 kWh.
Gas hob cooking, electric oven/microwave use and weekend-heavy laundry motivated
this work, but no appliance identities or individual appliance powers are inferred.
Source meter imports are not automatically equivalent to this component's boundary.

## Model and configuration

Copy `configs/regular_usage.example.toml` into ignored `data/` for private scenarios.
The example allocates annual energy as follows. These are editable **assumptions**,
not measured shares or estimates of achievable flexibility:

| Component | Annual share | Shape |
| --- | ---: | --- |
| Background | 0.35 | Continuous, constant energy per elapsed hour |
| Lighting | 0.08 | Dark and occupied hours; configurable daylight dependence |
| Routine activity | 0.42 | Smooth morning, daytime and evening activity |
| Evening shift | 0.10 | Separate budget initially allocated to evening hours |
| Weekend shift | 0.05 | Separate budget initially allocated to weekend hours |

Every component uses nonnegative weights `w[t]`, allocated as
`annual_total_kwh * share * w[t] / sum(w)`. Shares must sum to one.
Overlapping time windows do not duplicate energy: each component owns a distinct
budget. Changing annual kWh scales all components; changing a share requires
adjusting another share. Zero component budgets are allowed.

Lighting uses a documented NOAA fractional-year approximation to sun elevation
at each interval midpoint ([equations](https://gml.noaa.gov/grad/solcalc/solareqns.PDF)),
with representative configurable coordinates 52.1 N,
5.18 E. It is a binary darkness proxy, not measured illumination/cloud cover.
Lighting weights are `occupied * ((1-strength) + strength*dark)`, using local waking
hours (start inclusive, end exclusive). Strength 1 restricts lighting to darkness;
strength 0 gives equal lighting weights throughout occupied hours. Occupancy uses
hour-start labels. The approximation is restricted to latitudes within +/-66 degrees.

Routine weights sum three circular Gaussian peaks with configurable local-hour
centres, widths and strengths. A weekend offset moves all three routine peaks.
Routine and flexible source weights also use
`1 + amplitude*cos(2*pi*(day_of_year-peak_day)/days_in_year)`.
Lighting's seasonality comes from darkness; continuous background stays constant.
Neither peak labels nor fitted parameters identify real household activities.

## Moving energy

For each flexible component and scheduling group:

`shifted = (1-fraction_moved)*baseline + fraction_moved*group_budget*destination_weight/sum(destination_weight)`

The evening component groups by local calendar day. The weekend component groups
by Monday-start local scheduling week, clipped to the modeled year. The baseline
budgets are calculated once and never renormalized after shifting.

Source/destination hour weights contain 24 entries (00:00 through 23:00); day
weights contain seven entries (Monday through Sunday). Their products define
eligibility and relative preference. Overnight windows use entries on both sides
of midnight. DST repeated hours are both eligible; missing clock hours do not
exist. A positive moved budget with no destination within its day/partial week
fails with an explanation. With zero movement, destination availability is irrelevant.

Defaults move nothing. The example's destination is 12:00–16:00 on all days,
allowing every partial boundary week to allocate its budget. Set weekend destination
day weights to zero for weekday-only shifting when boundary weeks permit it.
Background, lighting and routine are unchanged by either movement fraction.

`shift_summary.txt` distinguishes scheduled transfer (fraction times component
budget) from net reallocation (half the absolute baseline/shifted difference).
Overlapping source and destination windows can make net reallocation smaller.
The morning/afternoon/evening/night changes partition energy; the weekend column
is an additional overlapping diagnostic, not another summable time window.
No tariff optimization, cost savings, appliance constraints, or live control is implied.

## External evidence and calibration

Prepare both reference datasets, summaries and an optional shape fit:

```sh
pixi run regular-usage-reference --config configs/regular_usage.example.toml --cache data/regular_usage_source --output outputs/regular_usage_reference
```

Repeat with `--offline` to require the cached archives. Only Fluvius part 1 is
needed: about 197 MB compressed, plus about 790 MB extracted CSV. Liander archives
are much smaller. No new dependencies or credentials are required. Full preparation
and fitting take tens of seconds locally; run the model first with
`variability.samples = 0` in a local config for a smaller smoke run.

Sources:

- [Liander Zonnedael downloads](https://www.liander.nl/over-ons/open-data): 2013
  quarter-hour imports and exports, Wh converted to kWh. Publisher-corrected
  readings may already contain averages. Positive exports are excluded; absence
  of recorded export does not prove absence of PV. EV/heating technologies are unknown.
- [Fluvius household profiles](https://fluvius.opendatasoft.com/explore/dataset/1_50-verbruiksprofielen-dm-elek-kwartierwaarden-voor-een-volledig-jaar/):
  use the 2024 `geen_ZP` group, additionally requiring zero heat-pump, EV and PV
  indicators, zero exports, and residential classification. Indicator absence does
  not guarantee equipment absence. Known batteries were excluded by the publisher.

Reject incomplete, invalid, negative or zero-total household-years. Validate every
local day's quarter count and unique clock labels. Do not fill missing readings.
Liander has a nominal 96-quarter grid even on DST dates. Fluvius's timestamp strings
end in Z, but the legend specifies CET/CEST and its data omit the spring hour and
repeat the autumn hour. Treat both as documented local clock labels, not inferred
UTC timestamps. Exclude both transition days from clock-hour summaries; retain all
reported energy for annual normalization. These reference intervals are never
exported into the shared UTC integration format.

Normalize each household by its own annual mean hourly energy. This makes shape
comparisons independent of annual size and leap-year duration. Aggregate individual
household monthly/day-type/hour means with equal household weighting; report mean,
median and 10–90% spread. Monthly annual-energy shares retain actual month lengths.
Sources remain separate; spreads describe household heterogeneity, not confidence
intervals. Original archives stay intact; source URLs, hashes, license link, quality
exclusions and cache-record dates are saved. Pre-existing cache retrieval times are
marked unknown rather than invented.

A stable SHA256 hash of dataset and anonymous household ID assigns approximately
20% to the test subset, with entire households kept together. Only the Fluvius
training subset drives bounded least squares of eight shape parameters: seasonal
amplitude, routine morning/evening centres and widths, evening/daytime strengths,
and weekend offset. All 576 monthly x day-type x clock-hour cells have equal weight.
Annual shares, lighting assumptions, background and flexible budgets are not fitted.
Bound hits are reported, not interpreted as measured peak times. Liander is a
separate descriptive comparison because its technology boundary is uncertain.

Evaluate initial, calibrated and flat shapes on both subsets of both sources.
This is exploratory evaluation, not a pristine prospective generalization test:
example settings were refined while examining development summaries. A fit to
external pooled data does not validate this home's load, its decomposition, or
its movable fraction. See [recorded aggregate results](regular_usage_reference_results.md).

## Model commands and outputs

Run a calibrated expected baseline plus the default 30 paired variable scenarios:

```sh
pixi run regular-usage --config configs/regular_usage.example.toml --year 2026 --reference outputs/regular_usage_reference --calibration outputs/regular_usage_reference/calibration.json --output outputs/regular_usage_2026
```

Compare a full shift of the two assumed budgets, retaining identical random seeds:

```sh
pixi run regular-usage --config configs/regular_usage.example.toml --year 2026 --reference outputs/regular_usage_reference --calibration outputs/regular_usage_reference/calibration.json --evening-fraction-moved 1 --weekend-fraction-moved 1 --output outputs/regular_usage_shifted_2026
```

`--reference` supplies comparison evidence only. `--calibration` explicitly applies
saved fit parameters; omit it to explore your configured shapes freely. Calibration
records an assumption signature: changing structural assumptions requires refitting,
while annual energy, seeds, movement fractions and destination preferences may vary.
To edit fitted shapes manually, copy the fitted parameter values into a local config
and omit `--calibration`. Model operation without either flag is entirely offline.

Use the seeds listed in `run.json` to select the active samples when reusing an
output directory with a different sample count.

Root files describe expected shifted consumption; `baseline/` describes its paired
unshifted consumption. Both have shared `hourly_energy.txt`/`daily_energy.txt`,
component-resolved `components.txt`, and heatmaps with identical colour limits.
`shift_summary.txt` reports budgets, net movement and time-window changes.
`samples/seed_N/` contains each variable pair. `run.json` records effective config,
config hash, calibration, external sources and seeds.

Plots show daily calendar heatmaps, hour-by-date heatmaps, component contributions,
seasonal weekday/weekend hourly curves, monthly external comparisons, and daily
scenario spread. Repeated autumn hours are averaged only for heatmap display;
both intervals remain in energy tables. Source household bands are held-out
10–90% spreads, with annual means aligned to the target year for hourly display.

`model_metrics.txt` and `diagnostic_comparison.txt` compare January/July ratios,
morning/evening window maxima, evening share, weekend/weekday ratios, daily CV and
hourly quantiles with external household ranges. A window maximum need not be a
true local peak. Raw metric/split tables, quality flags and `reference_report.md`
are saved in the reference output directory. Metrics outside household ranges
are diagnostics, not automatic proof of failure or reasons to suppress results.

Variable profiles use seeded lognormal daily multipliers and correlated hourly
multipliers, with fixed component budgets renormalized annually. Background stays
constant. A single realization is generated before applying either shift, so
baseline and shifted samples differ only in allocation. Variability parameters
are explicit assumptions, not fitted distributions or forecast uncertainty.

## Migration and validation

Old multiplicative-bias configs fail with a migration message. Replace
`weekend_appliance_bias`, `evening_shift_bias`, `flexible_fraction` and the old hourly
arrays with the five sections in the new example. Bias multipliers have no exact
conversion to explicit budgets/fractions, so no silent conversion is performed.

Run `pixi run test`. Tests cover energy/unit normalization, missing/invalid source
profiles, component budgets, overlapping windows, daily and partial-week transfers,
zero/full movement, fixed-demand preservation, DST/leap years, deterministic seeds,
legacy rejection and exclusion of test-household values from fitting.
