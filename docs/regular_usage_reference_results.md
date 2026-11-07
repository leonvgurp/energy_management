# Regular electricity reference evidence

Generated from downloaded household readings by `pixi run regular-usage-reference`.
Each household is normalized by annual mean hourly consumption and weighted equally.
Household spreads describe heterogeneity, not confidence intervals or this home.

| Dataset | Split | Households | January/July median (10–90%) |
| --- | --- | ---: | --- |
| liander | train | 19 | 1.464 (1.237–1.896) |
| liander | test | 15 | 1.285 (0.939–2.100) |
| fluvius | train | 239 | 1.230 (0.929–1.826) |
| fluvius | test | 60 | 1.218 (0.810–2.144) |

Calibration fits eight overall shape parameters on Fluvius training households only.
Component shares and movement fractions remain user assumptions.

| Dataset | Split | Model | Relative-load cell RMSE |
| --- | --- | --- | ---: |
| fluvius | train | initial | 0.3745 |
| fluvius | train | calibrated | 0.1694 |
| fluvius | train | flat | 0.2987 |
| fluvius | test | initial | 0.3886 |
| fluvius | test | calibrated | 0.1755 |
| fluvius | test | flat | 0.2914 |
| liander | train | initial | 0.2966 |
| liander | train | calibrated | 0.2123 |
| liander | train | flat | 0.4371 |
| liander | test | initial | 0.2711 |
| liander | test | calibrated | 0.2116 |
| liander | test | flat | 0.4381 |

Shape parameters at fitted bounds: routine.morning_hour, routine.morning_width, routine.evening_hour, routine.evening_width.
Bound hits indicate restricted templates/assumed shares; fitted peak parameters are not measured behaviour.
Evaluation is exploratory: example settings were refined during development; this is not an untouched prospective test.

RMSE uses 576 equally weighted monthly × day-type × clock-hour cells.
Liander technologies are unknown; exporters are excluded, but remaining PV/EV/heating cannot be ruled out.
Liander uses a nominal clock grid; Fluvius labels have a misleading Z suffix but follow local DST.
Both sources omit DST days from clock-hour comparisons. Annual energy remains intact.
Fluvius absence indicators do not guarantee equipment absence.
Source hashes, URLs and quality exclusions are saved beside this report.
