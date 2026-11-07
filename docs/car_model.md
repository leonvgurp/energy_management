# Car energy budget

## Annual evening schedule

`pixi run car-schedule --config configs/car_schedule.example.toml --year 2026 --output outputs/car_schedule_example`

Configure `annual_meter_kwh`, `charging_power_kw`, `charge_start` (local HH:MM)
and `timezone`. Without `daily_cycle_kwh`, daily meter energy is annual kWh
divided by the number of calendar days. With a cycle, discrete charge sizes
repeat in shuffled blocks. Each entry has a 20% chance of replacement by a
random cycle entry, permitting varying charge frequency. For each month, choose
the closest of `monthly_candidates` candidate sequences to day-count-weighted monthly targets
(or optional 12 `monthly_meter_kwh` targets). `seed` makes the result reproducible.
Retain zero-charge days in each month when the cycle includes zero. Do not
rescale charge sizes: monthly totals approximate their targets. The default
synthetic motif contains zero days, small top-ups and larger charges; it has
no fitted weekday or driving-cycle interpretation. Incomplete screenshot
sessions and unscaled monthly bars cannot establish a two-month daily series.
`seasonal_amplitude` defaults to zero; the example uses 0.15 as an uncalibrated
sensitivity assumption. Monthly factor is
`1 + amplitude * cos(2*pi*(month-1)/12)`, peaking in January and lowest in July.
Monthly weights are `days_in_month * seasonal_factor`. Monthly targets are annual
kWh multiplied by each weight divided by the sum of all 12 weights. This preserves
the annual target and handles February, leap years and 30/31-day months explicitly.
Seasonality describes relative daily consumption; a shorter month has fewer days
of that consumption. Explicit `monthly_meter_kwh` overrides those target shares
because those values already specify complete monthly energy budgets.
Charge sizes are multiplied by the monthly factor, rounded to 0.5 kWh and capped
at `max_charge_kwh` (default 13 kWh). The monthly sequence search still chooses
approximate totals; actual annual energy can differ slightly from the target.
Set amplitude to zero for the documented flat-month baseline. This represents
seasonal charging demand without estimating weather or petrol substitution.

The cap is a charger-meter energy limit per modeled session, not battery usable
capacity. The user's stated hybrid reserve is recorded as context, not deducted
again from this cap; battery charging losses mean those quantities differ.
Duration is each day's kWh / charging kW.
Each hourly value is charging power multiplied by the elapsed overlap between
that hour and the charge session. No additional efficiency loss is applied.
`monthly_summary.txt` records targets, achieved totals, differences and zero days.

Outputs match the shared component contract: `hourly_energy.txt` has
`interval_start_utc`, `interval_start_local`, `energy_kwh`; `daily_energy.txt`
has `date`, `energy_kwh`. Both are TSV, including zero-charge hours. UTC intervals
remain unique on 23/25-hour local days. `example_day_energy.txt` provides one full
day. Plots: `hourly_heatmap_YEAR.png`, `daily_heatmap_YEAR.png` and
`example_day_profile.png`. `run.json` records inputs, config hash and assumptions.

This is an annual-average scenario, not reconstructed trips or measured charging
sessions. It assumes at most one charge per day, including weekends, an optional seasonal uplift,
and constant power without taper or standby. No time-varying prices, solar
dispatch or settlement rules are modeled. The start is configurable for a later
timing comparison; automatic P1-based solar dispatch is outside this model.

Run a synthetic example:

```sh
pixi run car --config configs/car.example.toml --output outputs/car_example
```

Keep household configs, exports and screenshot transcriptions in ignored `data/car/`.
Outputs belong in ignored `outputs/`. Source images remain unchanged. Do not add
names, charger IDs or account details to tracked files. `run.json` records config,
SHA256 and limitations; `scenarios.tsv` contains daily energy and cost budgets.

For usable capacity B (kWh), reference range R (km), round-trip distance D (km),
consumption factor f and wall-to-battery efficiency eta:

- Battery consumption c = B/R * f (kWh/km).
- Range = B/c; electric distance = min(D, B/c).
- Meter energy = electric distance * c / eta.
- Net electricity cost = meter energy * (import price - eligible fraction * ERE rate).

Each case assumes a full battery at departure and replenishment at home. The
distance beyond battery range is a diagnostic, not a prediction of petrol km:
the engine may run earlier, including for heating. No petrol cost is estimated.
Winter factors are sensitivity assumptions, not fitted confidence intervals.
Efficiency is also assumed until charger energy can be paired with battery data.
Measured wall-meter totals already include downstream charging losses: never
divide those totals by efficiency again.

## Screenshot interpretation and next observations

Read numeric session labels; unscaled chart heights do not supply exact daily
measurements. The visible session list is incomplete. A session timestamp does
not establish a charging duration, arrival time or driving distance. Multiple
sessions can reflect disconnections or additional driving; they need not mean
multiple full battery cycles. “All chargers” needs confirmation as home-only.
“Past Month” and “Past Year” are app windows, not assumed calendar totals.
The year view with activity starting late in the year is not an annual baseline.
Do not add overlapping windows or infer zero driving from zero charging.

For a monthly energy budget, obtain exact covered dates and a complete charging
export. For a driving/weather model also collect daily km, commuting weekdays,
holidays, other trips, electric share and petrol use. For hourly household
integration add plug-in/unplug times, charging power, available charging windows
and starting/ending state of charge. Preserve exported timezone/offset semantics;
ambiguous naive DST timestamps require clarification before interval allocation.
This module deliberately emits no invented hourly profile.

Compare matched km/routes across seasons before fitting temperature effects.
Calendar totals alone confound cold weather, mileage and petrol substitution.
Use a separate no-credit cost baseline and do not assume a current ERE payout
persists in later years. Eligibility must follow the actual agreement and approved
metered volume. Solar charging needs opportunity-cost accounting in a later
household integration; the flat import price here assumes grid-priced charging.

## Reference checks (27 October 2026)

- Hyundai lists a 13.8 kWh nominal battery for this Tucson PHEV generation;
  nominal capacity is distinct from usable energy:
  https://www.hyundai.news/uk/articles/press-releases/hyundai-motor-reveals-details-on-all-new-tucson-plug-in-hybrid.html
- Zeres advertises approximately EUR 0.13/kWh and describes eligibility checks;
  this is not a guaranteed future tariff: https://zeres.nl/
