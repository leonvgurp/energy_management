# Project Instructions

## Purpose

Build a tool to explore, explain, and model energy usage at home. The household
includes solar panels, a heat pump, general electricity usage, and a plug-in hybrid
car. Keep models understandable, reproducible, and tied to available measurements.

## Staged Workflow and Goals

1. Map and model each component separately: solar generation, heat pump demand,
   general usage, and car charging.
2. Combine the component models into an overview of household energy flows,
   consumption, generation, grid imports, and exports. Avoid double-counting loads.
3. Use the combined model to explore and quantify energy-use optimizations.
4. Assess whether a home battery is worth the investment, and which battery type,
   usable capacity, and power rating suit the household. Include costs, efficiency,
   degradation, tariff assumptions, and uncertainty in the comparison.

Work incrementally, with individual components developed in separate chats.
Record assumptions, model interfaces, decisions, and reproducible commands in the
repository so later chats can continue and integrate the work. Stay within the
current step unless the user requests broader implementation.

The eventual goal is an interactive dashboard for exploring scenarios and options.
Keep calculation code reusable independently of the dashboard.

## Repository Structure

The Python/Pixi project has modality models in `src/energy_management/components/`,
shared annual orchestration in `household.py`, configuration in `config.py`, and
shared calendars, weather, energy tables and plots in sibling modules. The annual
entry point is `pixi run energy`, using `configs/household.example.toml`. Keep one
self-contained config per full-year scenario. Source data belong in ignored
`data/<modality>/` or `data/overall/`; annual outputs split tables and plots by
modality plus `overall`. Preserve standalone component entry points and existing
private input paths. Keep the README and reproducible commands current.

## Modeling and Data

- Implement all calculations in Python using explicit mathematical or statistical
  methods. Do not substitute agent-generated arithmetic, intuition, or unsupported
  numerical claims for executed, reproducible code. Explain the equations,
  assumptions, and limitations behind model outputs.
- Distinguish measured data, user-provided facts, assumptions, and estimates.
- State units explicitly. Distinguish power (W or kW) from energy (Wh or kWh).
- Record the source, date range, time resolution, and timezone of input data.
  Handle daylight-saving transitions explicitly when processing interval readings.
- Make tariffs, appliance characteristics, and household assumptions configurable;
  avoid embedding them in calculations.
- Compare proposed changes against a documented baseline. Show uncertainty and
  avoid presenting estimated savings as guaranteed outcomes.
- Keep source measurements intact and make derived outputs reproducible.

## Explanations and Visualization

Use Matplotlib and Seaborn for simple initial exploratory and explanatory plots.
Use Plotly for dynamic, interactive dashboard visualizations.

Use explanatory bar plots, line plots, and dot plots where they clarify component
behavior, comparisons, or model results. Label axes and units, distinguish measured
from modeled values, and expose relevant assumptions. Generate analytical plots
from the Python data and models. Add interactive scenario controls as the dashboard
is developed.

## Environment and Tooling

- Use Pixi exclusively to manage packages and environments. Declare all dependencies
  in the Pixi manifest and commit the lockfile; do not use separate pip, uv, Poetry,
  or Conda installation workflows.
- Run project workflows through `pixi run` commands. Define reusable Pixi tasks for
  common workflows and document their exact commands in the README.
- Prefer modern, maintained tools where practical. Use Polars for tabular data
  processing instead of pandas; use pandas interoperability only when a dependency
  requires it.
- When establishing the environment, include NumPy and SciPy for numerical and
  statistical work, statsmodels and scikit-learn for statistics and basic modeling,
  and PyMC and ArviZ for Bayesian probabilistic modeling and diagnostics. Include
  Polars, Matplotlib, Seaborn, and Plotly as specified above. Manage these packages
  through Pixi and verify imports before describing the environment as ready.

## Implementation and Validation

Use the simplest tools that meet the task. Once dependencies exist, document a
reproducible environment and exact commands for setup, execution, and validation.
Keep changes focused and preserve unrelated work.

Validate calculations with small, explainable examples, including unit conversions,
missing readings, and time boundaries where relevant. Run available checks after
changes and report what was verified and any remaining limitations. Do not claim
tests passed when no tests were run.

## Privacy and Safety

Do not commit credentials, API tokens, account identifiers, addresses, or private
household consumption exports. Add appropriate ignore rules before introducing
local data or secrets; use sanitized examples in documentation.

Treat electrical installation changes as proposals requiring qualified review.
Do not operate connected devices or change live energy settings without explicit
user authorization.
