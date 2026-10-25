"""Assumption-driven hourly space-heating scenarios; no automatic calibration."""
import argparse
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
from scipy.interpolate import RegularGridInterpolator

from .heat_pump_weather import bounds, validate_weather
from .energy_tables import write_energy_tables


def load_config(path):
    with open(path, 'rb') as stream:
        config = tomllib.load(stream)
    validate_config(config)
    return config


def validate_config(c):
    bounds(c)
    h, o, p = (c[k] for k in ('house', 'operation', 'performance'))
    required = {
        'house': ['setpoint_c', 'heat_loss_kw_per_k', 'thermal_capacity_kwh_per_k', 'internal_gains_kw', 'solar_aperture_m2', 'wind_loss_fraction_per_m_s', 'initial_indoor_c'],
        'operation': ['curve', 'curve_multiplier', 'min_flow_c', 'max_flow_c', 'heating_cutoff_c', 'recovery_hours', 'response_hours', 'initial_heating_kw', 'emitter_kw_per_k', 'auxiliary_active_kw', 'auxiliary_standby_kw', 'cop_multiplier', 'capacity_multiplier'],
    }
    for section, keys in required.items():
        for key in keys:
            if not np.isfinite(c[section][key]):
                raise ValueError(f'{section}.{key} must be finite')
    for key in ('heat_loss_kw_per_k', 'thermal_capacity_kwh_per_k'):
        if h[key] <= 0:
            raise ValueError(f'{key} must be positive')
    if not h['heated_areas_m2'] or any(not np.isfinite(x) or x <= 0 for x in h['heated_areas_m2']):
        raise ValueError('heated_areas_m2 must contain positive finite areas')
    if any(h[k] < 0 for k in ('internal_gains_kw', 'solar_aperture_m2', 'wind_loss_fraction_per_m_s')):
        raise ValueError('Gains, aperture and wind factor cannot be negative')
    if o['min_flow_c'] > o['max_flow_c'] or any(o[k] <= 0 for k in ('recovery_hours', 'response_hours', 'emitter_kw_per_k', 'cop_multiplier', 'capacity_multiplier')):
        raise ValueError('Invalid flow bounds or nonpositive operating parameter')
    if any(o[k] < 0 for k in ('curve', 'curve_multiplier', 'auxiliary_active_kw', 'auxiliary_standby_kw', 'initial_heating_kw')):
        raise ValueError('Curve and auxiliaries cannot be negative')
    for key in ('summer_off_start', 'summer_off_end'):
        date.fromisoformat('2000-'+o[key])
    if p['boundary_policy'] != 'clamp_and_flag':
        raise ValueError('Only explicit clamp_and_flag performance policy is supported')
    for key in ('outdoor_c', 'flow_c'):
        axis = np.asarray(p[key])
        if len(axis) < 2 or not np.isfinite(axis).all() or not (np.diff(axis) > 0).all():
            raise ValueError('Performance axes must be finite and strictly increasing')
    shape = (len(p['outdoor_c']), len(p['flow_c']))
    for key in ('cop', 'capacity_kw'):
        a = np.asarray(p[key])
        if a.shape != shape or not np.isfinite(a).all() or (a <= 0).any():
            raise ValueError(f'Invalid performance {key} map')
    if not p['source'].strip():
        raise ValueError('Performance source is required')
    if set(c['scenarios']) != {'low', 'base', 'high'}:
        raise ValueError('Define low/base/high scenarios explicitly')
    for s in c['scenarios'].values():
        if any(not np.isfinite(s[k]) or s[k] <= 0 for k in ('heat_loss_factor', 'thermal_capacity_factor', 'cop_factor')):
            raise ValueError('Scenario factors must be positive and finite')
        if not isinstance(s['shutdown_shift_days'], int) or abs(s['shutdown_shift_days']) > 60:
            raise ValueError('Shutdown shift must be integer days within +/-60')


def summer_off(day, operation, shift):
    # Positive shift = later shutdown, earlier restart (longer heating season).
    start = date.fromisoformat(f'{day.year}-'+operation['summer_off_start']) + timedelta(days=shift)
    end = date.fromisoformat(f'{day.year}-'+operation['summer_off_end']) - timedelta(days=shift)
    if start >= end:
        raise ValueError('Summer shutdown must be a nonempty within-year interval')
    return start <= day < end


def flow_target(outdoor, house, operation):
    return float(np.clip(house['setpoint_c'] + operation['curve']*operation['curve_multiplier']*(house['setpoint_c']-outdoor), operation['min_flow_c'], operation['max_flow_c']))


def simulate(weather, config, scenario='base'):
    """Exact one-hour integration of C dT/dt = Q + G - H(T-To).

    Idealized controller recovers the indoor deficit on a configurable timescale.
    The lumped temperature includes slab/building storage, not separate room zones.
    """
    validate_config(config)
    start, end = bounds(config)
    validate_weather(weather, start, end)
    h, o, p, s = (config[k] for k in ('house', 'operation', 'performance', 'scenarios'))
    s = s[scenario]
    zone = ZoneInfo(config['weather']['timezone'])
    capacity = h['thermal_capacity_kwh_per_k']*s['thermal_capacity_factor']
    loss = h['heat_loss_kw_per_k']*s['heat_loss_factor']
    indoor = h['initial_indoor_c']
    previous_request = o['initial_heating_kw']
    cop_map = RegularGridInterpolator((p['outdoor_c'], p['flow_c']), np.array(p['cop']))
    cap_map = RegularGridInterpolator((p['outdoor_c'], p['flow_c']), np.array(p['capacity_kw']))
    # Evaluate maps vectorially; hourly dynamics remain sequential.
    temps = weather['outdoor_c'].to_numpy()
    flows = np.array([flow_target(t, h, o) for t in temps])
    points = np.column_stack((np.clip(temps, min(p['outdoor_c']), max(p['outdoor_c'])), np.clip(flows, min(p['flow_c']), max(p['flow_c']))))
    cops = cop_map(points)*o['cop_multiplier']*s['cop_factor']
    caps = cap_map(points)*o['capacity_multiplier']
    rows = []
    for i, w in enumerate(weather.iter_rows(named=True)):
        t, cop, cap, flow = temps[i], float(cops[i]), float(caps[i]), flows[i]
        local = datetime.fromisoformat(w['interval_start_utc']).astimezone(zone)
        enabled = not summer_off(local.date(), o, s['shutdown_shift_days']) and t < o['heating_cutoff_c']
        H = loss*(1+h['wind_loss_fraction_per_m_s']*w['wind_m_s'])
        gains = h['internal_gains_kw']+h['solar_aperture_m2']*w['radiation_kwh_m2']
        decay = np.exp(-H/capacity)
        one_minus = -np.expm1(-H/capacity)
        target = indoor+(h['setpoint_c']-indoor)*(-np.expm1(-1/o['recovery_hours']))
        requested = max(0.0, H*(target-t-decay*(indoor-t))/one_minus-gains) if enabled else 0.0
        # Effective delivery/control lag, separate from building heat storage.
        requested = previous_request+(requested-previous_request)*(-np.expm1(-1/o['response_hours'])) if enabled else 0.0
        previous_request = requested
        emitter = max(0.0, o['emitter_kw_per_k']*(flow-indoor))
        delivered = min(requested, cap, emitter)
        next_indoor = t+(indoor-t)*decay+(delivered+gains)/H*one_minus
        baseline_heat = max(0.0, H*(h['setpoint_c']-t)-gains) if enabled else 0.0
        aux = o['auxiliary_active_kw'] if delivered > 0 else o['auxiliary_standby_kw']
        rows.append(dict(interval_start_utc=w['interval_start_utc'], interval_start_local=local.isoformat(), date=local.date().isoformat(), scenario=scenario,
                         consumption_kwh=delivered/cop+aux, outdoor_c=float(t), humidity_pct=w['humidity_pct'], wind_m_s=w['wind_m_s'], radiation_kwh_m2=w['radiation_kwh_m2'], flow_target_c=float(flow), cop=cop,
                         requested_heat_kwh=requested, delivered_heat_kwh=delivered, unmet_heat_kwh=requested-delivered,
                         capacity_shortfall_kwh=max(0.0, requested-cap), emitter_shortfall_kwh=max(0.0, min(requested, cap)-emitter),
                         indoor_start_c=indoor, indoor_end_c=next_indoor, gains_kwh=gains, loss_kwh=delivered+gains-capacity*(next_indoor-indoor), storage_change_kwh=capacity*(next_indoor-indoor), auxiliary_kwh=aux,
                         baseline_consumption_kwh=baseline_heat/cop+(o['auxiliary_active_kw'] if baseline_heat > 0 else o['auxiliary_standby_kw']),
                         heating_enabled=enabled, performance_boundary=bool(t != points[i, 0] or flow != points[i, 1])))
        indoor = next_indoor
    return pl.DataFrame(rows)


def reference_comparisons(all_rows, reference):
    results = []
    for ref in reference.get('observations', []):
        start, end = ref['start'], ref['end']
        # Reference date bounds are local, end exclusive; partial reading-day ambiguity is explicit metadata.
        for scenario in ('low', 'base', 'high'):
            selected = all_rows.filter((pl.col('scenario') == scenario) & (pl.col('date') >= start) & (pl.col('date') < end))
            expected = (date.fromisoformat(end)-date.fromisoformat(start)).days
            complete = selected['date'].n_unique() == expected
            observed = ref['electricity_kwh']
            modeled = selected['consumption_kwh'].sum() if complete else None
            yield_kwh = ref.get('environmental_yield_kwh')
            results.append(dict(label=ref['label'], scenario=scenario, coverage_complete=complete,
                                observed_electricity_kwh=observed, modeled_electricity_kwh=modeled,
                                difference_kwh=None if modeled is None else modeled-observed,
                                implied_heat_kwh=None if yield_kwh is None else yield_kwh+observed,
                                implied_period_cop=None if yield_kwh is None or observed <= 0 else (yield_kwh+observed)/observed,
                                scope=ref['scope'], uncertainty=ref['uncertainty']))
    return results


def run(config_path, weather_dir, output, reference_path=None):
    config = load_config(config_path)
    weather_dir, output = Path(weather_dir), Path(output)
    weather_path = weather_dir/'weather.txt'
    weather = pl.read_csv(weather_path, separator='\t')
    provenance = json.loads((weather_dir/'weather.json').read_text())
    if provenance['station'] != config['weather']['station'] or provenance['timezone'] != config['weather']['timezone']:
        raise ValueError('Weather provenance station/timezone does not match configuration')
    output.mkdir(parents=True, exist_ok=True)
    frames = []
    for scenario in config['scenarios']:
        frame = simulate(weather, config, scenario)
        frames.append(frame)
        dest = output/scenario
        dest.mkdir(exist_ok=True)
        write_energy_tables(frame, dest, 'consumption_kwh', config['weather']['timezone'])
        frame.select('interval_start_utc', 'interval_start_local', 'consumption_kwh').write_csv(dest/'hourly_consumption.txt', separator='\t')
        frame.write_csv(dest/'diagnostics.txt', separator='\t')
        daily = frame.group_by('date').agg(pl.col('consumption_kwh').sum().alias('daily_kwh')).sort('date')
        daily.write_csv(dest/'daily_consumption.txt', separator='\t')
        daily.with_columns(pl.col('date').str.slice(0, 7).alias('month')).group_by('month').agg(pl.col('daily_kwh').sum().alias('consumption_kwh')).sort('month').write_csv(dest/'monthly_summary.txt', separator='\t')
    combined = pl.concat(frames)
    summary = combined.with_columns(pl.col('date').str.slice(0, 4).alias('year')).group_by('scenario', 'year').agg(pl.col('consumption_kwh').sum(), pl.col('baseline_consumption_kwh').sum(), pl.col('unmet_heat_kwh').sum(), pl.col('performance_boundary').sum().alias('boundary_hours')).sort('year', 'scenario')
    summary.write_csv(output/'scenario_summary.txt', separator='\t')
    reference = tomllib.loads(Path(reference_path).read_text()) if reference_path else None
    metadata = dict(schema_version=1, method='assumption-driven one-node thermal model; uncalibrated', config=config,
                    weather=provenance, weather_sha256=hashlib.sha256(weather_path.read_bytes()).hexdigest(),
                    assumptions=['Current settings applied to all years: counterfactual, especially commissioning period.', 'Lumped effective thermal mass; no separate floor, room or controller states.', 'No hot water, cooling, boiler, explicit cycling or defrost events.', 'Humidity retained for diagnostics; no inferred humidity correction.', 'Low/base/high are bundled sensitivity scenarios, not confidence bounds or guaranteed ordering.', 'First hours depend on configured initial temperature and heating request; no hidden warm-up.', 'No automatic fitting or rescaling; controller counters are reference estimates.'],
                    reference=reference, comparisons=reference_comparisons(combined, reference) if reference else [])
    (output/'run.json').write_text(json.dumps(metadata, indent=2, allow_nan=False)+'\n')
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--weather', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--references', type=Path)
    a = p.parse_args()
    print(run(a.config, a.weather, a.output, a.references))


if __name__ == '__main__':
    main()
