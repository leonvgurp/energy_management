# Paired household bootstrap analysis

This analysis assembles the saved household component inputs and resamples the
locally cached KNMI De Bilt observations (2015–2025) into 100 alternative annual
weather realizations. It holds the calendar at 2027 for both policy regimes.
These are possible annual outcomes under a stationary historical climate, not
consecutive future years. All source measurements remain unchanged.

Run from the repository root:

```sh
pixi run household-analysis-prepare
pixi run household-analysis
pixi run test
```

Preparation requires the existing private heat-pump and car configs, solar
statistics output, and regular-use run metadata. It writes a self-contained
household config and a weather-sampling config into ignored `data/overall/`.
It uses the saved general-use shape (including its exploratory external fit),
with no shifting, and the supplied GreenChoice rates from the integrated config.
No separate car ERE receipts are included in the electricity bill.

The analysis writes to ignored `outputs/household_bootstrap_100/`. Its `report.md`
explains assumptions; four PNG figures show seasonal energy, bill lines and annual
spread, seasonal policy benefit, and load import costs versus solar value. TSV
annual/monthly run and summary tables retain seeds, means, sample standard
deviations, standard errors and 5/50/95 percentiles. Weather members, sampled source
blocks, config snapshots, hourly means/std and source/code hashes are retained.

Each weather year is resampled jointly in seven-day UTC blocks, with a ±21-day
seasonal window and a 3°C limit on temperature jumps at joins. The same hourly
household flows are settled both with and without annual saldering. Monthly PV
targets remain fixed at saved 2023–2025 mean yields; weather radiation controls
daily distribution, not interannual PV totals. Car and general-use annual budgets
are also fixed. This spread excludes tariff, equipment and structural model
uncertainty.

Two timing views are deliberately distinct: monthly annual-bill contributions
allocate netting credits across imports, following the existing cost model;
export-origin policy benefits assign the paired annual benefit across the months
whose exports create it. Export fee treatment is unchanged between regimes.
Neither view is a monthly invoice. Load import costs allocate shared hourly
imports in proportion to each load's consumption; they are not marginal savings
from removing that appliance. Solar operating value is avoided imports plus net
export value with loads and rates held fixed, excluding investment costs.

This focused analysis currently requires the saved single-rate contract with one
linear gross-export fee. The general cost model supports broader configurations.
