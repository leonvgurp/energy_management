# Solar scenario model

## Inputs and what they identify

TOML `monthly_kwh` contains twelve mean monthly energy totals T_m; annual energy
is their sum. `max_daily_kwh` supplies twelve daily ceilings C_m, not expected
sample maxima. `max_power_kw` is the separate instantaneous power ceiling P_max.
Locality, scenario year, and timezone determine astronomical daylight boundaries.

Monthly means and maxima cannot identify daily variance, clear-day frequency, or
multi-day cloudy spells. The model assumes a scaled Beta distribution for daily
energy. Its default concentration k=2 is configurable (`beta_concentration`).
It is an assumption, not a fit to the private example export. The statistics method requires only the summary config and calendar. The alternative
history method derives its inputs and distribution from a historical hourly table.

## Seeded daily allocation

For n days in a month with nonzero target:

    p = T_m / (n*C_m)
    alpha = p*k; beta = (1-p)*k
    q_i = (i+0.5+U_i)/n, U_i ~ Uniform(-0.2, 0.2)
    weight_i = Beta(alpha, beta).inverse_cdf(q_i)

Use one quantile stratum per day, then permute the weights with the supplied
random seed. This limits distribution changes between seeds. Zero-energy months
produce zeros; p=1 is a degenerate constant distribution. Impossible totals fail.

Each day also has a daylight power limit:

    day_cap_i = min(C_m, P_max * daylight_hours_i * (1-1e-8))
    energy_i = min(day_cap_i, lambda*weight_i)
    choose lambda so sum(energy_i) = T_m

A bracketed scalar root solves the exact monthly budget. The tiny capacity margin
avoids an unattainable perfect rectangle with nonzero power at sunrise/sunset.
Numerical zero weights have a machine-epsilon floor. The bounded adjustment can
slightly change the assumed Beta distribution. No impossible target is silently
reduced. Monthly totals remain fixed in leap years too, even when February has
another day. Daylight-specific limits may reduce attainable daily maxima.

## Historical alternative

`pixi run solar-history` requires 2–5 complete years in the documented regular
naive-hour TSV format. For each month, average the individual years' monthly
energy totals to get T_m; use the largest observed daily energy as C_m. Pool the
daily energy observations for that month across source years. Replace the Beta
inverse CDF above with the interpolated empirical quantile of those observations.
Keep exactly the same stratification, seed streams, permutation and bounded
monthly-budget adjustment. Both methods therefore share caps, intraday curves,
output schemas and plotting code. No yearly mean input is required.

The historical method captures observed marginal distributions better, but does
not reproduce weather sequences, cloudy spells or future changes to the panels.
Budget scaling and daylight caps can alter the sampled distribution. With all-zero
source production, supply a positive physical `--max-power-kw` explicitly.
The adapter groups supplied date labels without resolving source DST ambiguity;
metadata records this limitation and the required timezone/start-label assumption.
Generated output timestamps remain DST-aware. The curve parameters are identical
in both methods; history improves daily allocation, not intraday cloud modeling.

## Intraday power

The curve is zero outside daylight, with a Gaussian peak at solar noon. A plain
Gaussian never reaches zero, so subtract its daylight-boundary value. With
sunrise r, solar noon p, sunset s, and width fraction a (default 0.45):

    u = (p-t)/(p-r) before noon; (t-p)/(s-p) after noon
    b = exp(-1/(2*a^2))
    g(t) = [exp(-u^2/(2*a^2))-b]/(1-b), within daylight
    P(t) = min(P_max, A*g(t))
    choose A so integral(P(t), r, s) = energy_day

Increasing amplitude raises the shoulders and broadens a clipped plateau while
preserving daily energy. The integral of P in kW over hours is energy in kWh.
The two halves accommodate unequal sunrise-to-noon and noon-to-sunset durations.
The curve is continuous; its derivative can jump at the daylight/plateau edges.
The reusable `gaussian_power` and `gaussian_interval_energy` evaluate power and
analytical interval energy. The latter integrates plateaus piecewise; Gaussian
tails use erfc for numerical stability. Width is a configurable assumption.

## Solar calendar, timestamps and limits

Locality lookup uses [Open-Meteo](https://open-meteo.com/en/docs/geocoding-api);
ambiguous names require country information or coordinates. A yearly request to
[Sunrise-Sunset.org](https://sunrise-sunset.org/api) supplies dated, offset-aware
sunrise, sunset and solar noon. Raw responses, request URLs and retrieval times
are cached. Repeated/offline runs reuse those responses. Dates, coordinates,
timezone offsets and event order are checked; missing/polar events fail.

Hourly output is built on UTC intervals between consecutive local midnights,
then labeled with both UTC and local ISO timestamps. This preserves duplicate
fall-back clock hours and skips nonexistent spring-forward hours. DST changes
inside daylight and non-hour timezone transitions are not supported and fail
explicitly. They do not occur in the demonstrated Netherlands daylight periods.

This is a smooth scenario generator. It does not represent cloud timing, weather
forecasting, panel orientation, local shading, inverter-loss physics, degradation,
or year-to-year energy uncertainty. Fixed monthly totals deliberately remove
annual yield variability. A generated maximum need not equal its configured cap.
Daily allocation randomness is not a measure of forecast uncertainty.

The unchanged private hourly export can supply historical generation, the optional
comparison, and local summary inputs. Comparisons assume local interval-start labels unless told
otherwise, integrate modeled kW over each hour, and report kWh errors. They use
the known measured daily total, so energy agreement is imposed, not predicted.

## Adjusting day-to-day contrast

The existing `beta_concentration` controls spread at a fixed monthly mean and
daily ceiling: larger values produce fewer extreme days; smaller values increase
contrast. It does not smooth adjacent dates or change the capped intraday curve.
The generic default remains 2; means and maxima cannot identify this parameter.

The local derived-statistics example uses 2.879, fitted as one shared parameter
across all months to the supplied 2023–2025 daily distributions. The objective is
mean monthly Wasserstein distance across five seeds, evaluated over twenty seeds.
This is in-sample calibration, not evidence of forecast improvement. The calibrated
value is retained in the generated `input_statistics.toml`; temporary calibration
scripts and reports were removed during output cleanup. For recalibration, minimize
the mean of the twelve monthly Wasserstein distances between measured daily
yields and generated yields across seeds 0–4, then evaluate seeds 0–19.
Users without measurements can adjust concentration as a scenario assumption;
it cannot be inferred reliably from monthly energy budgets alone.
