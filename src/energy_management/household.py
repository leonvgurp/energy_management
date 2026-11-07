"""Annual household orchestration; all energy accounting is in electrical kWh."""
import argparse
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path

import numpy as np
import polars as pl

from .calendar import calendar_hours
from .config import REFERENCE_YEAR, general_config, heat_pump_config, load_config
from .components import boiler, general, heat_pump, phev, solar
from .energy_tables import hourly_energy, write_energy_tables
from . import weather

MODALITIES = ('solar', 'heat_pump', 'boiler', 'phev', 'general')
CONSUMPTION = tuple(f'{m}_consumption_kwh' for m in MODALITIES[1:])
ENERGY_COLUMNS = ('solar_production_kwh', *CONSUMPTION, 'total_production_kwh',
                  'total_consumption_kwh', 'net_balance_kwh', 'grid_import_kwh',
                  'grid_export_kwh', 'self_consumption_kwh')


def combine(components, year, timezone_name, high_consumption_quantile=.95):
    """Require exact complete-year alignment; never fill missing energy with zero."""
    utc, local = calendar_hours(year, timezone_name)
    frame = pl.DataFrame(dict(interval_start_utc=[t.isoformat() for t in utc],
                              interval_start_local=[t.isoformat() for t in local]))
    if set(components) != set(MODALITIES):
        raise ValueError('Require exactly solar, heat_pump, boiler, phev and general')
    for name in MODALITIES:
        component = hourly_energy(components[name], 'energy_kwh', timezone_name)
        if not component.select('interval_start_utc', 'interval_start_local').equals(frame.select('interval_start_utc', 'interval_start_local')):
            raise ValueError(f'{name}: component must cover exactly the full reference calendar')
        column = f'{name}_{"production" if name == "solar" else "consumption"}_kwh'
        frame = frame.with_columns(component['energy_kwh'].alias(column))
    if not 0 < high_consumption_quantile <= 1:
        raise ValueError('high_consumption_quantile must be in (0, 1]')
    frame = frame.with_columns(
        pl.col('solar_production_kwh').alias('total_production_kwh'),
        pl.sum_horizontal(CONSUMPTION).alias('total_consumption_kwh'))
    frame = frame.with_columns((pl.col('total_production_kwh')-pl.col('total_consumption_kwh')).alias('net_balance_kwh'))
    threshold = float(np.quantile(frame['total_consumption_kwh'].to_numpy(), high_consumption_quantile))
    return frame.with_columns(
        (-pl.col('net_balance_kwh')).clip(lower_bound=0).alias('grid_import_kwh'),
        pl.col('net_balance_kwh').clip(lower_bound=0).alias('grid_export_kwh'),
        pl.min_horizontal('total_consumption_kwh', 'total_production_kwh').alias('self_consumption_kwh'),
        (pl.col('net_balance_kwh') > 0).alias('overproduction'),
        (pl.col('total_consumption_kwh') >= threshold).alias('high_consumption'))


def daily_totals(hourly):
    columns = [name for name in ENERGY_COLUMNS if name in hourly.columns]
    return hourly.with_columns(pl.col('interval_start_local').str.slice(0, 10).alias('date')).group_by('date', maintain_order=True).agg(
        *[pl.col(name).sum() for name in columns], pl.len().alias('hours'))


def simulate(config, year=REFERENCE_YEAR, config_dir=Path('.')):
    """Reuse each modality model with a single calendar and shared weather."""
    s, w = config['scenario'], config['weather']
    zone, seed = s['timezone'], s['seed']
    events = weather.solar_events(year, zone, w['latitude'], w['longitude'])
    climate = weather.generate(config, year, events, config_dir)
    solar_config = deepcopy(config['solar'])
    response = solar_config.pop('weather_response', None)
    # Legacy scenarios retain explicit budgets; radiation-response scenarios use
    # already-adjusted shared weather, so sunshine is never applied twice.
    solar_config['monthly_kwh'] = [value*w['sunshine_factor']*factor
        for value, factor in zip(solar_config['monthly_kwh'], w['monthly_sunshine_factors'])]
    daily_weather = climate.with_columns(pl.col('interval_start_local').str.slice(0,10).str.to_date().alias('date')).group_by('date').agg(pl.col('radiation_kwh_m2').sum()).sort('date')
    if response is not None:
        solar_config['monthly_kwh'] = solar.weather_budgets(
            config['solar']['monthly_kwh'], daily_weather, response).tolist()
    sc = solar.SolarConfig(**solar_config)
    solar_daily = solar.generate_year(sc, events, seed, daily_weather=daily_weather)
    solar_hourly = solar.hourly_yields(solar_daily, events, sc, zone)
    hp = heat_pump.simulate(climate, heat_pump_config(config, year), config['heat_pump']['scenario'])
    hot_water = boiler.simulate(dict(config['boiler'], timezone=zone), date(year,1,1), date(year+1,1,1))
    charging = phev.generate(dict(config['phev'], timezone=zone, seed=seed), year)
    base_general, shifted_general = general.generate_pair(general_config(config), year, seed=seed)
    raw = dict(solar=(solar_hourly, 'yield_kwh', 60), heat_pump=(hp, 'consumption_kwh', 60),
               boiler=(hot_water, 'consumption_kwh', 30), phev=(charging, 'energy_kwh', 60),
               general=(shifted_general, 'energy_kwh', 60))
    components = {name: hourly_energy(frame, column, zone, minutes) for name, (frame, column, minutes) in raw.items()}
    hourly = combine(components, year, zone, config['plots']['high_consumption_quantile'])
    baseline_components = dict(components, general=hourly_energy(base_general, 'energy_kwh', zone))
    baseline = combine(baseline_components, year, zone, config['plots']['high_consumption_quantile'])
    diagnostics = dict(weather=climate, solar=solar_daily.join(events, on='date'), heat_pump=hp,
                       boiler=boiler.hourly_diagnostics(hot_water, zone), phev=charging, general=shifted_general)
    return hourly, components, diagnostics, baseline


def run(config_path, output, year=REFERENCE_YEAR, days=None, make_plots=True):
    config_path, output = Path(config_path), Path(output)
    config = load_config(config_path)
    # Source measurements and configs may never live under a generated directory.
    inputs = [config_path.resolve()]
    if config['weather']['mode'] == 'file':
        inputs.append((config_path.parent/config['weather']['file']).resolve())
    if any(path == output.resolve() or output.resolve() in path.parents for path in inputs):
        raise ValueError('Output directory must not contain config or source weather')
    hourly, components, diagnostics, baseline = simulate(config, year, config_path.parent)
    root = output/'data'
    overall = root/'overall'
    overall.mkdir(parents=True, exist_ok=True)
    hourly.write_csv(overall/'hourly_energy.txt', separator='\t')
    daily_totals(hourly).write_csv(overall/'daily_energy.txt', separator='\t')
    baseline.write_csv(overall/'baseline_hourly_energy.txt', separator='\t')
    for name, frame in components.items():
        write_energy_tables(frame, root/name, 'energy_kwh', config['scenario']['timezone'])
    for name, frame in diagnostics.items():
        dest = root/name
        dest.mkdir(parents=True, exist_ok=True)
        frame.write_csv(dest/'diagnostics.txt', separator='\t')
    (output/'scenario.toml').write_bytes(config_path.read_bytes())
    summary = {column: float(hourly[column].sum()) for column in ENERGY_COLUMNS}
    summary.update(hours=hourly.height, overproduction_hours=int(hourly['overproduction'].sum()),
        high_consumption_threshold_kwh=float(np.quantile(hourly['total_consumption_kwh'].to_numpy(), config['plots']['high_consumption_quantile'])),
        peak_consumption_kwh=float(hourly['total_consumption_kwh'].max()),
        baseline_grid_import_kwh=float(baseline['grid_import_kwh'].sum()),
        baseline_grid_export_kwh=float(baseline['grid_export_kwh'].sum()))
    if 'costs' in config:
        from .costs import comparison
        costs = dict(scenario=comparison(hourly, config['costs'], config['scenario']['timezone']),
                     baseline=comparison(baseline, config['costs'], config['scenario']['timezone']))
        (overall/'annual_costs.json').write_text(json.dumps(costs, indent=2, allow_nan=False)+'\n')
        summary['annual_cost_eur'] = costs['scenario']['selected']['total_eur']
        summary['without_minus_with_saldering_eur'] = costs['scenario']['without_minus_with_eur']
        summary['baseline_minus_scenario_cost_eur'] = costs['baseline']['selected']['total_eur']-costs['scenario']['selected']['total_eur']
    metadata = dict(schema_version=1, config=config, reference_calendar_year=year, synthetic=True,
        source_sha256={str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs},
        implementation_sha256={str(path.relative_to(Path(__file__).parent)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(Path(__file__).parent.rglob('*.py'))},
        summary=summary, assumptions=[
            'Reference calendar labels a recurring scenario, not a prediction for the label year.',
            'All modality outputs are modeled electrical kWh, not measured consumption; EUR costs are estimates at configured tariffs.',
            'General consumption excludes space heating, boiler and PHEV charging.',
            'Heat pump models space heating only; boiler is an independent hot-water unit.',
            'Solar uses capped Gaussian curves and shared daily radiation; configured weather_response scales monthly budgets and bounds annual yield when enabled.',
            'Monthly-statistics weather and equipment settings are illustrative, not calibrated household observations.',
            'Net balance = production - consumption; positive means surplus. No battery or subhourly netting.',
            'Initial building temperature and zero boiler recovery debt are model boundary assumptions; final recovery debt is retained in diagnostics.',
            'Baseline differs only in general-use shifting, using the identical weather and random realization.',
            'Winter cold offset affects space heating; boiler COP and PHEV seasonality remain explicit independent assumptions.'])
    if make_plots:
        from .plots import plot_outputs
        metadata['selected_days'] = plot_outputs(hourly, output/'plots', config['plots'], days,
            weather=diagnostics['weather'], solar_events=diagnostics['solar'])
    (output/'run.json').write_text(json.dumps(metadata, indent=2, allow_nan=False)+'\n')
    return metadata


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=Path('configs/household.example.toml'))
    p.add_argument('--output', type=Path, default=Path('outputs/household'))
    p.add_argument('--calendar-year', type=int, default=REFERENCE_YEAR, help='Calendar labels only; default 2001, not a forecast')
    p.add_argument('--day', action='append', help='MM-DD or YYYY-MM-DD; repeat to override default seasonal examples')
    p.add_argument('--no-plots', action='store_true')
    a = p.parse_args()
    result = run(a.config, a.output, a.calendar_year, a.day, not a.no_plots)
    print(json.dumps(dict(output=str(a.output), **result['summary']), indent=2))


if __name__ == '__main__':
    main()
