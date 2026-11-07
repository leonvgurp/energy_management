"""Seasonal regular electricity with independent, energy-conserving shift budgets."""
import argparse
import calendar
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
from scipy.signal import lfilter

from energy_management.energy_tables import write_energy_tables

from ..calendar import calendar_hours, darkness

COMPONENTS = ('background', 'lighting', 'routine', 'evening_shift', 'weekend_shift')
LEGACY = {'weekend_appliance_bias', 'evening_shift_bias', 'flexible_fraction',
          'fixed_hour_weights', 'flexible_hour_weights'}


def validate(config):
    if LEGACY.intersection(config):
        raise ValueError('Legacy regular-usage configuration: replace multiplicative biases with '
                         'five component shares, source/destination weights and fraction_moved; '
                         'see configs/regular_usage.example.toml and docs/regular_usage_model.md')
    def number(value, lo, hi, name):
        if isinstance(value, bool) or not np.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f'{name} must be finite in [{lo}, {hi}]')
    number(config['annual_total_kwh'], 0, np.inf, 'annual_total_kwh')
    ZoneInfo(config['timezone'])
    for name in COMPONENTS:
        number(config[name]['share'], 0, 1, name+'.share')
    if not np.isclose(sum(config[n]['share'] for n in COMPONENTS), 1, rtol=0, atol=1e-10):
        raise ValueError('Component shares must sum to one')
    number(config['seasonality']['amplitude'], 0, .95, 'seasonality.amplitude')
    number(config['seasonality']['peak_day'], 1, 366, 'seasonality.peak_day')
    light = config['lighting']
    number(light['latitude'], -66, 66, 'latitude')
    number(light['longitude'], -180, 180, 'longitude')
    number(light['wake_hour'], 0, 23, 'wake_hour')
    number(light['bed_hour'], 1, 24, 'bed_hour')
    if light['wake_hour'] >= light['bed_hour']:
        raise ValueError('Lighting wake_hour must precede bed_hour')
    number(light['seasonal_strength'], 0, 1, 'lighting.seasonal_strength')
    routine = config['routine']
    for key in ('morning_hour', 'evening_hour', 'daytime_hour'):
        number(routine[key], 0, 24, key)
    for key in ('morning_width', 'evening_width', 'daytime_width'):
        number(routine[key], .1, 12, key)
    for key in ('morning_weight', 'evening_weight', 'daytime_weight'):
        number(routine[key], 0, 10, key)
    number(routine['weekend_offset_hours'], -6, 6, 'weekend_offset_hours')
    for name in COMPONENTS[-2:]:
        c = config[name]
        number(c['fraction_moved'], 0, 1, name+'.fraction_moved')
        for prefix in ('source', 'destination'):
            for suffix, length in (('hour_weights', 24), ('day_weights', 7)):
                weights = np.asarray(c[prefix+'_'+suffix], dtype=float)
                if weights.shape != (length,) or not np.all(np.isfinite(weights)) or np.any(weights < 0):
                    raise ValueError(f'{name}.{prefix}_{suffix}: require {length} finite nonnegative weights')
    v = config['variability']
    for key in ('daily_sigma', 'hourly_sigma'):
        number(v[key], 0, 2, key)
    number(v['hourly_correlation'], 0, .99, 'hourly_correlation')
    if type(v['samples']) is not int or not 0 <= v['samples'] <= 1000:
        raise ValueError('samples must be an integer from 0 to 1000')
    if type(v['seed']) is not int or v['seed'] < 0:
        raise ValueError('seed must be a nonnegative integer')


def _allocate(weights, budget):
    if budget == 0:
        return np.zeros(len(weights))
    if not np.all(np.isfinite(weights)) or weights.sum() <= 0 or np.any(weights < 0):
        raise ValueError('Positive component budget requires positive finite source weights')
    return weights * (budget/weights.sum())


def _transfer(source, destination_weights, groups, fraction):
    if fraction == 0:
        return source.copy()
    result = source*(1-fraction)
    for group in dict.fromkeys(groups):
        indices = np.flatnonzero(groups == group)
        budget = source[indices].sum()*fraction
        if budget > 0:
            if destination_weights[indices].sum() <= 0:
                raise ValueError(f'No destination capacity for positive shift budget in {group}; '
                                 'include valid hours/days within each day or partial week')
            result[indices] += _allocate(destination_weights[indices], budget)
    return result


def generate_pair(config, year, seed=None):
    """Return component-resolved baseline and shifted frames using identical draws."""
    validate(config)
    utc, local = calendar_hours(year, config['timezone'])
    hours = np.array([t.hour for t in local])
    weekdays = np.array([t.weekday() for t in local])
    days = np.array([t.date().isoformat() for t in local])
    weeks = np.array([(t.date()-timedelta(days=t.weekday())).isoformat() for t in local])
    doy = np.array([t.timetuple().tm_yday for t in local])
    year_days = (datetime(year+1, 1, 1)-datetime(year, 1, 1)).days
    s = config['seasonality']
    seasonal = 1+s['amplitude']*np.cos(2*np.pi*(doy-s['peak_day'])/year_days)
    noise = np.ones(len(hours))
    if seed is not None:
        rng = np.random.default_rng(seed)
        _, inverse = np.unique(days, return_inverse=True)
        v = config['variability']
        daily = rng.normal(0, v['daily_sigma'], inverse.max()+1)[inverse]
        rho = v['hourly_correlation']
        innovations = rng.normal(0, v['hourly_sigma']*np.sqrt(1-rho*rho), len(hours))
        hourly = lfilter([1], [1, -rho], innovations)
        noise = np.exp(daily+hourly)
    light, routine = config['lighting'], config['routine']
    occupied = (hours >= light['wake_hour']) & (hours < light['bed_hour'])
    light_weights = occupied*((1-light['seasonal_strength'])+light['seasonal_strength']*
                              darkness(utc, light['latitude'], light['longitude']))
    routine_weights = np.zeros(len(hours))
    offset = (weekdays >= 5)*routine['weekend_offset_hours']
    for peak in ('morning', 'daytime', 'evening'):
        delta = (hours+.5-routine[peak+'_hour']-offset+12) % 24-12
        routine_weights += routine[peak+'_weight']*np.exp(-.5*(delta/routine[peak+'_width'])**2)
    weights = dict(background=np.ones(len(hours)), lighting=light_weights*noise,
                   routine=routine_weights*seasonal*noise)
    destinations = {}
    for name in COMPONENTS[-2:]:
        c = config[name]
        weights[name] = (np.asarray(c['source_hour_weights'])[hours]*
                         np.asarray(c['source_day_weights'])[weekdays]*seasonal*noise)
        destinations[name] = (np.asarray(c['destination_hour_weights'])[hours]*
                              np.asarray(c['destination_day_weights'])[weekdays])
    baseline = {n: _allocate(weights[n], config['annual_total_kwh']*config[n]['share']) for n in COMPONENTS}
    shifted = {n: a.copy() for n, a in baseline.items()}
    for name, groups in (('evening_shift', days), ('weekend_shift', weeks)):
        shifted[name] = _transfer(baseline[name], destinations[name], groups,
                                  config[name]['fraction_moved'])
    def frame(values):
        return pl.DataFrame(dict(interval_start_utc=[t.isoformat() for t in utc],
                                 interval_start_local=[t.isoformat() for t in local], **values,
                                 energy_kwh=sum(values.values())))
    return frame(baseline), frame(shifted)


def generate(config, year):
    """Compatibility entrypoint: deterministic shifted scenario."""
    return generate_pair(config, year)[1]


def shift_summary(baseline, shifted, config):
    rows = []
    hours = np.array([datetime.fromisoformat(t).hour for t in baseline['interval_start_local']])
    weekdays = np.array([datetime.fromisoformat(t).weekday() for t in baseline['interval_start_local']])
    windows = dict(night=hours < 6, morning=(hours >= 6)&(hours < 12),
                   afternoon=(hours >= 12)&(hours < 17), evening=hours >= 17,
                   weekend=weekdays >= 5)
    for name in (*COMPONENTS, 'energy_kwh'):
        b, s = baseline[name].to_numpy(), shifted[name].to_numpy()
        row = dict(component=name, baseline_kwh=float(b.sum()), shifted_kwh=float(s.sum()),
                   net_reallocated_kwh=float(np.abs(s-b).sum()/2))
        row['scheduled_transfer_kwh'] = (float(b.sum()*config[name]['fraction_moved'])
                                         if name in COMPONENTS[-2:] else 0.)
        if name == 'energy_kwh':
            row['scheduled_transfer_kwh'] = sum(float(baseline[n].sum()*config[n]['fraction_moved'])
                                                 for n in COMPONENTS[-2:])
        for label, mask in windows.items():
            row[label+'_change_kwh'] = float((s-b)[mask].sum())
        rows.append(row)
    return pl.DataFrame(rows)


def write_pair(baseline, shifted, output, config):
    output = Path(output)
    results = []
    for frame, folder in ((baseline, output/'baseline'), (shifted, output)):
        results.append(write_energy_tables(frame, folder, 'energy_kwh', config['timezone']))
        frame.write_csv(folder/'components.txt', separator='\t')
    shift_summary(baseline, shifted, config).write_csv(output/'shift_summary.txt', separator='\t')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--year', type=int, default=2026)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--reference', type=Path, help='Prepared external summaries for comparison plots')
    parser.add_argument('--calibration', type=Path, help='Explicit optional shape-calibration JSON; config shares stay fixed')
    parser.add_argument('--evening-fraction-moved', type=float, help='Override evening shift fraction for this run')
    parser.add_argument('--weekend-fraction-moved', type=float, help='Override weekend shift fraction for this run')
    args = parser.parse_args()
    config = tomllib.loads(args.config.read_text())
    validate(config)
    for name, value in [('evening_shift', args.evening_fraction_moved),
                        ('weekend_shift', args.weekend_fraction_moved)]:
        if value is not None:
            config[name]['fraction_moved'] = value
    calibration_record = None
    if args.calibration:
        calibration_path = args.calibration
        calibration_record = json.loads(calibration_path.read_text())
        if args.reference and 'household_cells_sha256' in calibration_record:
            with (args.reference/'household_cells.txt').open('rb') as stream:
                reference_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
            if reference_hash != calibration_record['household_cells_sha256']:
                raise ValueError('Reference summaries do not match calibration source fingerprint')
        from energy_management.regular_usage_reference import assumption_signature
        if calibration_record['assumption_signature'] != assumption_signature(config):
            raise ValueError('Calibration assumptions changed: rerun regular-usage-reference with this config '
                             '(annual total, movement fractions and destination weights may vary without refitting)')
        for section, values in calibration_record['parameters'].items():
            config[section].update(values)
    validate(config)
    baseline, shifted = generate_pair(config, args.year)
    [(bh, bd), (sh, sd)] = write_pair(baseline, shifted, args.output, config)
    from energy_management.regular_usage_plots import model_metrics
    metrics = [dict(scenario='expected_baseline', seed=-1, **model_metrics(baseline)),
               dict(scenario='expected_shifted', seed=-1, **model_metrics(shifted))]
    sample_daily = []
    for i in range(config['variability']['samples']):
        seed = config['variability']['seed']+i
        b, s = generate_pair(config, args.year, seed)
        [_, (_, daily)] = write_pair(b, s, args.output/'samples'/f'seed_{seed}', config)
        sample_daily.append(daily['energy_kwh'].to_numpy())
        metrics.extend([dict(scenario='variable_baseline', seed=seed, **model_metrics(b)),
                        dict(scenario='variable_shifted', seed=seed, **model_metrics(s))])
    pl.DataFrame(metrics).write_csv(args.output/'model_metrics.txt', separator='\t')
    from energy_management.regular_usage_plots import plot_outputs
    plot_outputs(baseline, shifted, bd, sd, sample_daily, args.output, args.year, args.reference)
    record = dict(status='Assumption-based; external comparison is not household validation',
                  year=args.year, config=config,
                  external_sources=json.loads((args.reference/'sources.json').read_text()) if args.reference else None,
                  baseline_annual_kwh=baseline['energy_kwh'].sum(),
                  shifted_annual_kwh=shifted['energy_kwh'].sum(), calibration=calibration_record,
                  config_sha256=hashlib.sha256(args.config.read_bytes()).hexdigest(),
                  seeds=[config['variability']['seed']+i for i in range(config['variability']['samples'])],
                  variability='Correlated lognormal hourly and daily multipliers; assumed, not fitted',
                  boundaries='No appliances, tariffs, equipment limits or guaranteed achievable flexibility')
    (args.output/'run.json').write_text(json.dumps(record, indent=2)+'\n')
    print(f'Wrote expected pair and {len(sample_daily)} paired samples to {args.output}; '
          f'annual total {shifted["energy_kwh"].sum():.3f} kWh')


if __name__ == '__main__':
    main()
