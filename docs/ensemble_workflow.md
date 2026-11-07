# Expected household energy and cost across synthetic years

`pixi run energy` is the main workflow. It reads `configs/ensemble.example.toml`,
which points to the complete household model configuration. The default is 100
full-year simulations under identical assumptions with distinct seeds. This is a
**Monte Carlo ensemble of synthetic years**, sometimes called a parametric
bootstrap, rather than a nonparametric bootstrap of measured years. It does not
recalibrate or fit any component.

```sh
pixi run energy
pixi run energy --config data/overall/ensemble.toml
pixi run energy --config configs/ensemble.example.toml --output outputs/trial --no-plots
pixi run energy-year --config configs/household.example.toml --output outputs/one_year
pixi run energy-plots --input outputs/ensemble --day 03-25
```

## Configuration and choices

Minimal workflow config (all paths are relative to this workflow file):

```toml
[ensemble]
scenario_config = "household.toml"
# Optional settings, shown with defaults:
runs = 100
seed = 42
calendar_year = 2001
output = "../outputs/ensemble"
plots = true

# Optional: complete detailed configs for alternative choices.
[alternatives]
daytime_charging = "daytime_charging.toml"
shifted_general = "shifted_general.toml"
```

For a workflow stored in `data/overall/`, use `output = "../../outputs/my_ensemble"`
to write under the repository's outputs directory. CLI `--output` is relative to
the working directory. CLI `--no-plots` overrides `plots`. Omit `[alternatives]`
when evaluating just one household config. Costs are required in every config;
unknown workflow settings and invalid counts are rejected.

`seed` is the first realization seed: run i uses `seed + i`, starting at zero.
The workflow overrides the detailed config's `scenario.seed`. Increasing the run
count preserves the previous seed prefix. Alternative configs use the same seeds,
reference calendar and timezone. The detailed configs remain self-contained:
copy one and change charging time, shifting fractions, heat-pump parameters,
solar size, or tariffs to explore a choice. Weather-file paths remain relative to
the detailed household config, not the workflow config. Comparisons using different
tariffs include both the energy-choice and tariff effects.

Each config also has an internal `baseline`, which disables **general-use shifting
only** and uses identical weather and random draws. The default example shifts
nothing, so its baseline savings are zero. Use alternatives for other interventions.
For meaningful comparisons, document which detailed assumptions differ. Common
seed labels help pair realizations; changing a stochastic model's structure can
also change how its random draws are consumed.

## Calculation and uncertainty

For realization r and hour h, the existing household model computes production,
consumption, hourly import/export, and self-consumption. Annual settlement,
including annual saldering and export-fee thresholds, is applied **to that
realization**, never to the mean hourly profile. With N runs:

```text
mean hourly energy[h] = sum(energy[r,h]) / N
expected annual bill = sum(annual_bill[r]) / N
paired choice savings[r] = reference_bill[r] - alternative_bill[r]
sample std = sqrt(sum((value[r] - mean)^2) / (N-1))
standard error of mean = sample std / sqrt(N)
```

All quantities and ratios, including annual peaks and self-consumption fractions,
are computed per realization before averaging. A mean annual peak is not the peak
of the mean profile. The output std uses N-1; std and standard error are blank for
N=1. Quantiles use linear interpolation. `p05` and `p95` describe variation across
**simulated years**, conditional on the inputs; they are not confidence bounds on
the mean or guarantees about observed years. Paired differences have their own
std/quantiles, rather than subtracting independent uncertainty bounds.

Annual solar, PHEV and general-use budgets and monthly irradiation are constrained
by their detailed configs. Seeds vary timing/weather patterns and heating demand,
not those fixed budgets. Boiler schedules can be entirely deterministic. File-mode
weather reuses the same file across seeds. This workflow therefore does not claim
to cover uncertainty in annual sunshine, equipment, household behavior, tariffs,
or future climate. Explore those assumptions with separate configs. Reference
calendar 2001 supplies weekdays and DST, not historical 2001 weather or a forecast.

Mean hourly import and export may both be positive: different realizations can
have different flow directions in that hour. Do not re-net mean consumption and
production, or rebill averaged grid flows, to reproduce expected costs.

## Tables and plots

All `.txt` tables are TSV. Root outputs describe the reference config. Alternatives
have the same layout under `scenarios/<name>/`.

| Output path | Content |
| --- | --- |
| `data/overall/hourly_energy.txt` | Mean electrical kWh by modality, totals and already-netted grid flows; UTC/local timestamps; surplus/high-consumption probabilities |
| `data/overall/hourly_energy_std.txt` | Sample std per elapsed hour for every energy column |
| `data/overall/daily_energy.txt` | Local-date sums of hourly means; 23/24/25 elapsed hours |
| `data/overall/baseline_*` | Same hourly/daily mean and hourly std tables for general-use baseline |
| `data/overall/annual_runs.txt` | Each run/seed, variant, metric, unit and value: energy, peaks, fractions, annual bills, netted/exported quantities and individual bill lines |
| `data/overall/annual_summary.txt` | Samples, mean, std, standard error, p05/p50/p95 for every annual metric and variant |
| `data/overall/monthly_runs.txt` | Monthly energy totals and allocated bill lines for every run/variant/regime |
| `data/overall/monthly_summary.txt` | The same summary statistics by local month |
| `data/overall/choices_summary.txt` | Annual summaries across all named configs |
| `data/overall/choice_differences_runs.txt` | Paired reference minus alternative metrics, including annual costs, for shared metric names |
| `data/overall/choice_differences_summary.txt` | Summary of those paired differences; positive EUR means the alternative saves money |
| `data/<modality>/{hourly,daily}_energy.txt` | Common component table contract, containing means |
| `data/weather/diagnostics.txt` | Mean weather used for mean day profiles |
| `data/solar/diagnostics.txt` | Calendar sunrise/noon/sunset events used for plots |
| `plots/` | Existing modality/overall heatmaps and four seasonal day profiles, labeled as means; annual and monthly cost comparison plots |
| `scenario.toml`, `run.json` | Detailed config snapshot, mean summary, annual statistics and selected plot days |
| `workflow.toml`, `ensemble.json` | Workflow snapshot, effective settings, seed list, all source/implementation hashes and limitations |

`variant` is `scenario`, `baseline`, or `baseline_minus_scenario`. Positive EUR in
the latter means general shifting saves money. Metrics ending `_eur` are EUR;
`_kwh` are electrical kWh, `_fraction` are dimensionless, and `_hours` are elapsed
hours. Fixed annual cost line names include their position to distinguish repeated
names. Cost metrics contain `with_saldering` / `without_saldering` explicitly.
`without_minus_with_saldering_eur` isolates the policy toggle at fixed tariffs.

Daily and hourly means preserve linear energy identities. Selected winter/summer
best/worst days are extrema of **mean daily net energy**, not the average of each
realization's extreme days. Sun events are deterministic; outdoor temperatures
are averaged with the energy. Shared plotting can regenerate profiles from saved
mean data; the ensemble metadata preserves the mean labels.

Monthly cost values are **contributions to the annual settlement**, not bills
settled separately by month. Import charges follow actual monthly register usage;
annual netting credits follow monthly imports within the credited normal/offpeak
register. Annual export receipts and variable export charges follow monthly export
shares. Fixed daily charges follow days; annual fixed charges, export fixed fees
and selected tier fees are spread by calendar days. This explicit accounting
allocation exactly reconciles each annual bill line. It is not a supplier's invoice
schedule or a claim about the timing of cash payments.

## Saldering interpretation

Both regimes always use each detailed config's same tariffs. This isolates the
saldering effect; use an alternative config with updated prices for a separate
post-saldering contract scenario. The workflow does not infer a future tariff or
automatically select a regime from the reference calendar.

As checked on 2026-11-07, statutory saldering ends on 1 January 2027. Export still
receives remuneration; until 2030 the statutory minimum is 50% of the bare supply
rate (excluding taxes), not 50% of the model's tax-inclusive import rate.
See [Rijksoverheid](https://www.rijksoverheid.nl/themas/klimaat-milieu-en-natuur/energie-thuis/salderingsregeling)
and [the cost-model equations and contract assumptions](electricity_costs.md).

These are annual electricity operating costs. Capital costs, financing, maintenance,
battery behavior and investment returns are outside this workflow.
