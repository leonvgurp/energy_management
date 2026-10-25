# Sample realistic fictional weather years

This sampler generates **synthetic hourly weather**, which can drive the existing
heat-pump calculation. It is neither a forecast for the target date nor a climate
projection. It assumes the observed **2015–2025** reference climate remains applicable.
Those inclusive dates cover **11 calendar years**. No trend or warming correction
is applied, and no household energy totals are fitted.

## Reproducible workflow

```sh
pixi run weather-scenarios --config configs/weather_scenarios.example.toml --output outputs/weather_scenarios_2027_seed42
```

This downloads public De Bilt (260) hourly KNMI temperature, humidity, wind and
radiation into `data/weather_history_2015_2025/` using the existing raw-response
cache. It generates 30 independently seeded sample calendars in `member_000/`
through `member_029/`. The fictional target year is 2027; change it freely.
The example config is public and contains no household energy data.

Subsequent generation from the saved normalized history is fully offline:

```sh
pixi run weather-scenarios --config configs/weather_scenarios.example.toml --history data/weather_history_2015_2025 --reuse-history --output outputs/weather_scenarios_2027_seed42
```

Alternatively `--offline` reconstructs the observed history from cached raw responses
and fails if any cache entries are absent. Use separate output directories when
changing seeds/member counts; old member directories are not automatically deleted.
The returned report config's member count determines the current ensemble.

To generate consumption, make a private copy of your heat-pump config and set its
`weather.years = [2027]`, with empty `start`/`end`, matching the sampler target.
**Manual prerequisite:** keep station and timezone consistent with the sampler.
An existing local `data/heat_pump_synthetic_2027.toml` preserves the household
assumptions and has this target calendar set.

```sh
pixi run heat-pump --config data/heat_pump_synthetic_2027.toml --weather outputs/weather_scenarios_2027_seed42/member_000 --output outputs/heat_pump_synthetic_2027_seed42
pixi run heat-pump-plots --input outputs/heat_pump_synthetic_2027_seed42
pixi run test
```

Switch `member_000` to sample another year, keeping the same heat-pump settings.
No cost tariff is assumed: the current output is kWh; euro costs require a separately
specified tariff. Do not compare fictional dates to controller counters as if they
were a reconstruction of that year.

## Algorithm and configurable assumptions

The TOML `[weather]` section sets the target year(s), optional exclusive date bounds,
station and timezone. `[sampling]` defines:

| Key | Meaning; example default |
| --- | --- |
| `reference_years` | Contiguous observed source years, 2015–2025 inclusive |
| `seed` | First NumPy generator seed, 42; member i uses seed+i |
| `members` | Number of weather realizations, 30 |
| `block_days` | Consecutive UTC days copied per block, 7 |
| `seasonal_window_days` | Allowed source-start displacement around target month/day, ±21 days |
| `max_join_temperature_jump_c` | Maximum absolute temperature step at a block join, 3°C |
| `heating_degree_base_c` | Base for descriptive daily-mean degree-day checks, 18°C |
| `cold_day_threshold_c` | Daily-mean threshold for cold spells, 0°C |

For each target block, select uniformly from source blocks whose start month/day
is within the seasonal window. Measure circular calendar distance on a fixed
366-day leap calendar, so March does not shift between leap/nonleap years. Require
enough consecutive source hours for the entire block and a temperature-compatible
join to the preceding block. If no candidate exists, fail with advice to widen
the window or join tolerance; never silently substitute weather.

Copy all four variables together, hour for hour. This preserves real within-block
hourly profiles, variable correlations, and multi-day persistence. There is **no
Gaussian daily temperature assumption**, independent-hour sampling, smoothing,
interpolation or energy-budget adjustment. All generated values are traceable to
source observations. Longer `block_days` preserve longer weather sequences but
reduce recombination diversity. The join constraint changes the sampling distribution;
it prevents large artificial temperature jumps without making joins fully physical.

Blocks align at UTC midnight. Thus the solar/diurnal phase is preserved across DST
instead of stretching 23/25-hour source days. Output calendars use local bounds and
unique UTC hours with offset-aware local labels; first/last UTC blocks are cropped.
Leap target years have the correct number of elapsed hours. Incomplete UTC source
edge days are not sampled. Historical inputs must be complete; no gap filling occurs.

## Saved data and checks

Each member contains:

- `weather.txt`: UTC/local interval starts, four weather variables, source UTC
  timestamp and block ID. Directly compatible with the heat-pump weather reader.
- `weather.json`: `synthetic = true`, seed, resolved config, source SHA256 and full
  observed-history provenance, including KNMI units and observation conventions.
- `daily_weather.txt`: local daily mean/minimum/maximum temperature (°C), mean wind
  (m/s), humidity (%), radiation sum (kWh/m²), and elapsed-hour count.
- `sampled_blocks.txt`: source/target starts, length, candidate count and join step.

Ensemble-level annual/monthly check tables compare observed history to synthetic
members: seasonal means, daily-temperature percentiles, annual degree-days, cold-day
counts, longest calendar-year cold spells and hourly lag-one temperature correlation.
Daily degree-days are `sum(max(0, base - daily_mean_temperature))` in K·days; these
are descriptive, not the heat-pump demand equation. Partial years are not annualized.
`seasonal_comparison.txt` and `sampling_report.json` quantify monthly mean discrepancies.
`weather_validation.png` compares monthly temperatures and winter daily distributions;
`synthetic_daily_temperature.png` shows the first sample year.

These are **in-sample descriptive checks**, not proof of forecast skill or calibrated
probabilities. Seven-day blocks cannot preserve entire winter circulation regimes,
interannual persistence or longer cold-spell probabilities. Block seams can alter
humidity/wind relationships across the seam even when temperature is constrained.
The sampler cannot invent extremes beyond the source record; 11 years poorly
identify rare events. Use the generated members as plausible scenarios, not a
confidence interval or a guarantee that the tails represent future risk.

The fixed-window block method is deliberately simple and inspectable. If checks
show unacceptable seasonal drift or cold-spell distortion, investigate sampling
parameters before interpreting electricity ranges; do not tune to household totals.
