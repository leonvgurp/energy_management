# Shared energy table contract

All component generators call `energy_tables.write_energy_tables`.
Tables are UTF-8 TSV with header rows, chronological ordering, and no index.

- `hourly_energy.txt`: string `interval_start_utc`, string
  `interval_start_local`, Float64 `energy_kwh`, in that order.
- `daily_energy.txt`: ISO `date` string and Float64 `energy_kwh`.

An hourly timestamp labels the inclusive start of a one-elapsed-hour interval;
the end is exclusive. ISO timestamps include seconds and numeric offsets:
`2026-01-01T00:00:00+00:00` and `2026-01-01T01:00:00+01:00`.
UTC is the unique join key. The local zone is the configured zone, normally
Europe/Amsterdam. Daily aggregation uses local calendar dates, with 23/25 hours
on DST transitions. Different components may cover different years: align date
ranges explicitly, never silently replace missing coverage with zero.

`energy_kwh` is nonnegative interval energy, not kW and not a cumulative meter.
Solar files represent generation; space-heating and boiler files represent
electrical consumption. Regular-usage root tables represent the configured shifted
scenario; its `baseline/` folder and each paired sample use the same contract.
Reference household clock labels are descriptive inputs, never UTC integration
tables. Component identity and scenario are supplied by the
folder/run configuration, not inferred from the value. Do not add all components
with the same sign when computing grid flows. The annual runner performs the
strict alignment and household accounting described below.

The writer verifies finite, nonnegative values, consecutive intervals, UTC/local
consistency and complete hours. It sums boiler half-hour energy without rounding
the 08:30 draw to an hourly start. Solar and space heating already run hourly.
Missing intervals, duplicates, partial hours and incorrect offsets fail rather
than being filled or dropped. Only full-hour-aligned input timezones are supported.

Existing `hourly_yields.txt`, `hourly_consumption.txt`, and daily component tables
remain convenience interfaces; new cross-component consumers should read the
shared files. Boiler `daily_consumption.txt` now matches space heating's
`date`, `daily_kwh` schema. Boiler detailed heat and recovery information lives
in hourly `diagnostics.txt`, separate from electrical-energy integration.
Energy columns are summed over each hour; recovery debt is the end-of-hour state.
Half-hour calculation steps are internal only and are not exported.

## Integrated household outputs

`pixi run energy` now writes the five component contracts under
`outputs/household/data/<modality>/` and a wide hourly/daily balance under
`data/overall/`. See [the annual contract](household_model.md) for columns,
sign conventions, reference calendars, weather adjustments and baseline outputs.
