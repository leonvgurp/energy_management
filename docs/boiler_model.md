# aroSTOR domestic hot-water model

This component is separate from the space-heating heat pump. Household facts
supplied on 2026-10-25: aroSTOR VWL B200/5, **60°C setpoint** (corrected from
65°C), morning use finishing 08:30 and evening use finishing 19:00, every day.
These times are modeled recovery starts, not measured compressor switch times.

## Manufacturer reference

[Vaillant installation manual 0020285063_07](https://professional.vaillant.co.uk/downloads/aproducts/renewables-1/arostor-1/0020285063-00-arostor-200-270ltr-install-1456700.pdf),
retrieved 2026-10-25, sections F and technical data (printed pages 29–34).
This is the UK B200/5 variant; confirm regional equivalence with the installed
unit's plate before treating all figures as exact.

| Specification | B200/5 reference |
| --- | --- |
| Nominal tank volume | 200 litres |
| Heat pump thermal output, water at 45°C | 1.42 kW |
| Electric element | 1.2 kW |
| COP, air 7°C, EN16147 cycle L | 2.99 |
| Heat-up time, air 7°C, cycle L | 6.57 hours |
| Standby electrical test value Pes, air 7°C | 25 W |
| Permitted intake air | −7 to 45°C |

Section F describes a heat-pump water-temperature ceiling of 60°C up to
35°C intake air, falling to 55°C at 45°C air; the element supplies the gap to
higher targets. Controls can also enable element assistance at low tank
 temperatures or to meet a recovery-time limit. Actual mode settings are unknown.
The test COP, heat-up time and Pes are not a measured performance curve at
our 60°C setpoint. The example uses rounded COP 3 and constant 1.42 kW thermal
output as explicit approximations, not a fit to all those test figures.

## Interpretation and equations

The pressurized tank remains full: incoming cold water replaces water drawn.
“Empty” is provisionally interpreted as removal of one full tank's worth of
60°C hot-water-equivalent heat; “half” is half that. It does not mean 200/100
litres of mixed shower water. Actual usable hot water depends on stratification
and outlet temperature. Draws are instantaneous heat-budget events after use;
heating during showers and sensor hysteresis are omitted.

For tank volume V litres, fraction f, cold temperature Tc, target Ts and assumed
heat-pump limit Th:

- `Qhp = V f (4.186 / 3600) (Th - Tc)` thermal kWh.
- `Qelement = V f (4.186 / 3600) (Ts - Th)` thermal kWh.
- `E = Qhp / COP + Qelement` electrical kWh, ideal element efficiency 1.

Density is approximated as 1 kg/l. At the current 60°C target and limit,
Qelement is zero. Recovery jobs use heat pump first and element second, never
simultaneously, at constant thermal power. Overlapping jobs queue, and unfinished
energy carries across midnight; `outstanding_heat_kwh` reports recovery debt,
not measured shortage or tank temperature. Initial midnight recovery debt is zero.
This is a simple daily energy and timing model, not a tank/control simulation.

A separate uniform 25 W electrical allowance approximates standing/control costs.
It is configurable, not a measured heat leak at 60°C, and is not divided by COP
again. Set it to zero to isolate draw recovery. No pipe losses are modeled.
A 10°C mains supply, COP 3, power and this allowance are assumptions. Intake air
may be room or duct air, not outside weather. No seasonal dependence is inferred.
If room air supplies heat, later integration must account for its space-heating
impact; do not simply add this extracted heat as another electrical load.

## Reproduce and inspect

```sh
pixi run boiler --config configs/boiler.example.toml --start 2026-01-01 --end 2027-01-01 --output outputs/boiler_2026
pixi run test
```

Edit an ignored `data/boiler.toml` copy for private/calibrated inputs. Outputs:
`hourly_energy.txt`, `daily_energy.txt`, `hourly_consumption.txt`,
`daily_consumption.txt`, hourly `diagnostics.txt` (TSV), and `run.json`
with configuration, hash, manufacturer URL, date bounds and totals. Intervals
in the hourly tables are one elapsed hour, UTC-unique and offset-aware locally. DST days have 23/25
rows; draw times stay at local 08:30/19:00. The end date is exclusive. Uniform
standing allowance follows elapsed hours. Unsupported ambiguous overnight draw
times fail explicitly. Output files are replaced on rerun.

Half-hour steps exist only inside the calculation to preserve the 08:30 draw.
All saved interval tables and the daily profile plot use hours. Diagnostic energy
columns sum the internal steps; outstanding recovery heat is the end-of-hour
state, not a sum. Daily tables remain local-calendar daily totals.

Baseline is the supplied unchanged schedule. Explore COP 2.5–3.5 and mains
5–15°C as sensitivity assumptions, not statistical confidence limits. The most
valuable calibration is several days of appliance electricity and actual hot-water
use, followed by intake/mains temperatures and observed cycle duration. Do not
interpret the illustrative range as guaranteed consumption or savings.

## Hybrid installation boundary

User clarification, 2026-10-25: water leaving the aroSTOR passes through the CV
installation, which uses gas to top it up when too cool. This component excludes
that gas expenditure and any CV auxiliary electricity. It never adds downstream
heat demand to the aroSTOR recovery budget. The draw fractions remain assumed
aroSTOR hot-water-equivalent extraction, not a claim about total shower energy.
The downstream top-up means continued hot-water availability cannot identify
tank depletion. No plumbing changes or device settings are made.

## Executed baseline

The example 2026 run gives 2,341.07 kWh electrical for the year (6.414 per
24-hour day), including a 0.600 kWh/day standing allowance. Draw replacement is
17.442 thermal kWh/day. Constant-power recovery lasts 8.189 hours after the
morning draw and 4.094 hours after the evening draw. These timings approximate
08:30–16:41 and 19:00–23:06; actual control and temperature-dependent power
can change them. Sensitivity corners (mains 15°C/COP 3.5 and mains 5°C/COP 2.5)
give 5.085–8.274 electrical kWh per 24-hour day, with the same standing allowance.
These are scenarios, not measured consumption. The command also generates
`daily_profile.png` for the first simulated day.
