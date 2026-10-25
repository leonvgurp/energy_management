# Assumption-driven heat-pump scenarios

This component estimates **space-heating electricity**, independently of the solar
component and any dashboard. It is not an hourly reconstruction or a fitted model.
Observed KNMI weather drives configurable household/equipment assumptions. Controller
counters are optional reference estimates; they never rescale model output.

## Configuration

Copy `configs/heat_pump.example.toml` into ignored `data/` and edit it. The example
is sanitized and illustrative. Every numerical model parameter is explicit:

| Section | Parameters and units |
| --- | --- |
| `weather` | Locality, IANA timezone, KNMI station/name, contiguous years; optional local `start` and exclusive `end` dates |
| `house` | Setpoint/initial temperature (°C), heated areas (m², context only), heat loss H (kW/K), thermal storage C (kWh/K), internal gains (kW), effective solar aperture (m²), fractional wind-loss increase per m/s |
| `operation` | Curve/multiplier, min/max flow (°C), outdoor heating cutoff (°C), summer-off month-days, recovery/response time (hours), initial heating request (kW), emitter conductance (kW/K), active/standby additional electricity (kW), COP/capacity multipliers |
| `performance` | Ordered outdoor/flow axes (°C), rectangular COP and thermal-capacity (kW) matrices, source, boundary policy |
| `scenarios.low/base/high` | Multipliers on H, C and COP; summer shutdown shift in days |
| `manufacturer_reference` | Optional sourced test points, retained as context, **not** used by the simulator |

The private household configuration records supplied setpoint, areas and heating
settings. H, C, gains, emitter response, COP map, capacity and auxiliaries remain
**provisional assumptions**. House age/energy label do not determine these numbers.
The assumed performance grid is not manufacturer-certified. Reference points from
[Vaillant's /6 S2 manual](https://www.vaillant.ch/downloads/installationsanleitungen/deutsch/waermepumpen/installations-und-betriebsanleitung-arotherm-plus-vwl-35-75-2510805.pdf)
are identified separately; variant compatibility and full part-load performance
remain unverified. Test heating output is not automatically maximum capacity.

Low/high combine assumptions rather than assigning probabilities. Negative shutdown
shift starts summer-off earlier and restarts heating later; positive does the reverse.
The example uses ±14 days. Results need not be ordered in every hour. Adjust each
factor independently for sensitivity experiments. Current settings apply to all
weather years: this is counterfactual, especially during commissioning.

## Equations and limitations

For each elapsed hour, effective heat loss is
`H = heat_loss_kw_per_k * scenario_factor * (1 + wind_factor * wind_m_s)`.
Gains are internal kW plus effective solar aperture × hourly radiation kWh/m²
(numerically an average kW over one hour). No orientation model is implied.

The explicit **approximation**, not a reverse-engineered Vaillant controller, is
`flow = clip(setpoint + curve * curve_multiplier * (setpoint - outdoor), min, max)`.
The 0.5 curve number alone does not establish Vaillant's precise nonlinear curve.
A displayed target is not an actual measured water temperature. See
[Vaillant's controller curve explanation](https://www.vaillant.nl/producten-3/sensocomfort/sensocomfort-vrc-720-2-gebruiksaanwijzing-en-installatiehandleiding-0020288147-02483842-en-2569250.pdf).

A single lumped temperature represents effective building/floor thermal storage:

```
C dT/dt = delivered_heat + gains - H (T - outdoor)
a = exp(-H/C)                         # one-hour interval
T_next = outdoor + (T-outdoor)*a + (delivered_heat+gains)/H*(1-a)
T_requested = T + (setpoint-T)*(1-exp(-1/recovery_hours))
Q_ideal = max(0, H*(T_requested-outdoor-a*(T-outdoor))/(1-a) - gains)
Q_request = previous_request + (Q_ideal-previous_request)*(1-exp(-1/response_hours))
Q_delivered = min(Q_request, equipment_capacity, max(0, emitter_conductance*(flow-T)))
electricity_kWh = Q_delivered/COP + additional_auxiliary_kW * 1 hour
```

Heating cutoff and summer shutdown set the request to zero. Internal/solar gains
and passive cooling continue; no active cooling exists. There is no upper indoor
temperature cap. Finite recovery and request response are assumptions about effective
control/thermal lag, not detailed thermostat logic or a separate slab state.
Initial temperature and request are explicit; early results have startup effects.
No hidden spin-up, random daily curves, minimum-modulation cycling, explicit defrost,
boiler assistance, domestic hot water or Bayesian fitting is implemented.

COP and capacity use bilinear interpolation. Inputs outside the rectangular map
are clamped and every affected hour is flagged. This is visible sensitivity, not
validated extrapolation. Auxiliary electricity is **additional** to electricity
represented in COP; set it to zero for components already included in a source COP.
Humidity is preserved but does not trigger an unsupported defrost correction.

The degree-hour baseline uses `max(0, H*(setpoint-outdoor)-gains)/COP` plus auxiliaries
when enabled, without inertia or capacity limits. It is an explanatory comparison,
not a calibrated alternative. `unmet_heat_kwh` is requested minus delivered heat;
capacity and emitter shortfalls are split. Temperature shortfall may also result
from deliberate response lag; aggregate unmet heat alone does not describe comfort.

## Weather and time

[KNMI hourly observations](https://daggegevens.knmi.nl/klimatologie/uurgegevens)
from De Bilt (260) provide a nearby central-Netherlands station with coverage in
the demonstrated years. This is not an on-site temperature measurement. Station
selection is explicit; gaps do not silently select another station.

KNMI HH 1–24 labels the **end** of the interval: date+HH−1 hour becomes the UTC start.
T is temperature in tenths °C at observation time, U is relative humidity percent;
both endpoint samples are held over the preceding interval as an approximation.
FH is hourly mean wind in tenths m/s. Q is accumulated J/cm²; divide by 360 to get
kWh/m². Raw responses and retrieval metadata are cached. Extra API boundary days
are trimmed per request; duplicates or missing required hours fail, not filled.

Requested bounds are local midnights converted to UTC. DST days retain 23/25 elapsed
hours and repeated local hours have different offsets. Requested years must be
contiguous. For incomplete/current years, specify an exclusive end date for which
validated observations are available. Missing recent weather fails explicitly.

## Saved interfaces and reference comparisons

Each scenario directory contains TSV `hourly_consumption.txt`
(`interval_start_utc`, `interval_start_local`, `consumption_kwh`),
`daily_consumption.txt` (`date`, `daily_kwh`), `monthly_summary.txt`, and full
`diagnostics.txt`. A row is one elapsed hour, so hourly kWh is numerically equal
to average kW. The solar timestamp convention is preserved; consumption has its
own column name and is not signed generation.

`scenario_summary.txt` groups energy, baseline, unmet heat and map-boundary counts
by scenario and weather year. Partial years are not annualized. `run.json` records
the resolved config, source hashes/provenance, assumptions and optional comparisons.
Reusing an output directory replaces the modeled tables; prefer a separate directory
for each config/date range. Saved metadata and plots may contain private information.

Optional reference TOML:

```toml
[[observations]]
label = "Example controller heating estimate"
start = "2025-01-01"
end = "2026-01-01" # exclusive local date
electricity_kwh = 2000.0
scope = "Space heating only"
uncertainty = "Controller estimate; meter accuracy unknown"
# environmental_yield_kwh = 6000.0
```

Comparisons require full date coverage; otherwise modeled total/difference are null.
A reading taken during a day has an uncertain endpoint: state this in `uncertainty`.
Lifetime counters without a precise start/end belong in reference metadata, not an
invented comparison interval. Validate reference units and scopes before interpreting
implied efficiency. [Vaillant defines environmental yield](https://professional.vaillant.co.uk/downloads/product-brochures/22006-heat-pump-leaflet-v05-web-2-2343880.pdf)
as extracted environmental heat: implied heat = electricity + yield, and period
COP = implied heat / electricity. These are controller-derived checks, not independent
meter validation. No correlations or percentage-accuracy claims follow from them.

## Plots and validation

Offline plots include annual month × day heatmaps (base scenario, shared scale),
four real winter dates nearest the 10/35/65/90th daily-temperature percentiles,
and temperature-quartile median/10–90% hourly profiles. DJF summaries use complete
24-hour days only. Quartiles pool all requested DJF days, including separate season
fragments. Daily charts compare all scenarios and observed outdoor temperature.
Monthly scenario lines separate bundled parameter sensitivity from the recorded
weather-year variation; neither yields calibrated uncertainty bounds.

Tests cover analytical heat/energy accounting, response lag, bounds, capacity,
shutdown, missing data, unit conversions, leap/DST boundaries, cache-only reuse,
config sensitivity, deterministic output, aggregation and plot creation. To improve
confidence later, obtain independent dated electricity or heat measurements before
considering parameter calibration or Bayesian posterior inference.
