# Electricity costs / elektriciteitskosten

Research checked 2026-11-07. Supplied GreenChoice amounts are user-provided
contract inputs, not independently verified current public tariffs. There is no
validity date in the supplied list. The example assumes all import prices include
energy tax and VAT and all charges/credits are consumer cash amounts. Confirm this
against the contract: adding energy tax again would double count it. The supplied
EUR 0.15/kWh remuneration is used unchanged as a receipt without VAT, assuming a
private consumer who does not charge VAT. This is configurable, not tax advice.

## Supplier language mapped to calculations

| Contract construction | Normalized model |
| --- | --- |
| Per exported kWh, e.g. Greenchoice | `export_charges` with `basis="gross_export"`, `eur_kwh`; includes saldered export |
| Fee on only export surplus | `basis="excess_export"`; surplus depends on saldering toggle |
| Annual export band, e.g. Vattenfall | `tiers`: select one annual fee from exported kWh, never sum all previous bands |
| Daily solar/export surcharge | `eur_day` on an export charge; applied for whole year if its basis is positive |
| Fixed solar surcharge regardless of export | Ordinary `fixed` line, even for zero export |
| Monthly or annual fee | Normalize monthly fee to `12 * monthly` in `eur_year`; annual amount unchanged |
| Fee embedded in import price / fixed supply costs | Include it once in import rates / `fixed`; no second export charge |
| Net remuneration already after export fees | Use net `export_eur_kwh` and no matching fee; gross-plus-fee is preferable for auditability |
| Dynamic market price plus markup or percentage | Needs interval price series and explicit contract settlement; not represented by a constant annual price |

Sources: [Greenchoice per-kWh explanation](https://www.greenchoice.nl/zonnepanelen/terugleverkosten/),
[Vattenfall annual scales and fixed costs](https://www.vattenfall.nl/energie/leveringskosten-energie/),
[ACM on costs included in fixed or energy rates](https://consument.acm.nl/elektriciteit-en-gas/duurzame-energie/wat-is-salderen),
[Frank Energie on dynamic pricing and negative prices](https://www.frankenergie.nl/nl/kennisbank/dynamisch-energiecontract/dynamisch-energie-contract-terugleveren).

## Equations and settlement

Use **grid import** I and **grid export** E, not total household demand or solar
production. Direct self-consumption never enters either quantity. Annual saldering
volume S is `min(I,E)` with `saldering=true`, otherwise zero. Remunerated export is
`E-S`. With a single import rate p and export receipt r:

```
annual EUR = (I-S)*p - (E-S)*r + export charges + fixed charges
fixed charges = sum(days * eur_day + eur_year)
export charge = Q*eur_kwh + days*eur_day + eur_year + selected tier fee
```

Export charges apply only if their selected quantity Q is positive. Separate fixed
lines are unconditional. Tier upper bounds are inclusive, strictly increasing,
and the final tier must cover the quantity; exceeding it raises an error instead
of silently underbilling. The first tier covers positive quantities up to its bound.
Tier examples in the config illustrate the schema, not actual supplier quotes.

For dual rates the model calculates gross cost per normal/offpeak register, then
allocates S using `proportional`, `normal_first`, or `offpeak_first`. Cross-register
allocation is a contract assumption, not a claim about GreenChoice's method.
`meter="single"` is the default until the household contract is confirmed.
Weekends are offpeak; weekday hours and explicit MM-DD holiday dates are
configurable. No holiday calendar is silently assumed. Confirm 21:00 versus 23:00
and the allocation method with your supplier. DST uses actual Europe/Amsterdam
hours, including both repeated autumn hours. Fixed daily charges use local calendar
days (365 or 366), not elapsed hours divided by 24.

## 2026 versus 2027

[ACM](https://consument.acm.nl/elektriciteit-en-gas/duurzame-energie/wat-is-salderen)
states statutory saldering ends **1 January 2027**. From then all exported energy
receives remuneration. Until 2030 that remuneration must be at least 50% of the
agreed electricity supply price excluding energy tax and VAT. Export costs remain
separate, so the rule does not guarantee a positive net receipt after those costs.
The model does not infer a legal minimum from the supplied all-in prices: the
underlying supply price is not given.

`comparison()` calculates both toggle values on identical flows and tariffs.
This isolates saldering's effect; it is **not a forecast of the actual 2027 bill**.
For that forecast copy the full scenario and update remuneration, charges, tax
credit and import rates to the applicable 2027 contract. The boolean is independent
of the reference calendar year, which defaults to 2001 purely for synthetic energy
scheduling. Use `--calendar-year 2026` or 2027 to explore weekday differences too;
those two runs no longer isolate just the policy effect.

## Usage and outputs

```
pixi run energy-year --config configs/household.example.toml --output outputs/cost_example --no-plots
pixi run test
```

Set `[costs].saldering = false` to select the no-saldering result. Removing the entire
optional costs section preserves energy-only configs. The reusable Python entry
point is `energy_management.costs.annual_cost(hourly, config, timezone_name)`;
`comparison` computes both regimes. Input must be ordered complete-year hourly
flows with aware UTC timestamps and finite nonnegative energy; missing readings,
duplicates and incomplete years are rejected.

`data/overall/annual_costs.json` contains itemized EUR charges, credited and
remunerated kWh, selected and both policy totals, and the general-use baseline.
`run.json` and terminal output include selected annual cost, no-saldering minus
saldering cost, and baseline minus shifted-scenario cost. Config snapshots and code
hashes preserve the rates and implementation used. Calculations retain precision;
round to cents only for display, not per hour.

## Limits

This implementation supports constant yearly single/dual rates and composable
export fees. It does not claim to reproduce every supplier invoice: changing
contract periods, dynamic hourly/quarter-hourly prices, marginal block prices,
separate tax netting rules for dynamic contracts, partial-year invoices and
supplier-specific rounding need explicit extensions. A future dynamic adapter
must align timestamped prices with meter intervals, separate supply and energy
tax, specify the annual tax credit allocation and retain negative prices. A yearly
average market price would misvalue solar-weighted exports.

Energy inputs are synthetic and use hourly import/export netting. Real meter
flows within the hour can increase both import and export; therefore costs may be
underestimated. No guaranteed savings or calibrated household bill is implied.
