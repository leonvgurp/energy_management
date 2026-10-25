"""Seasonally matched multivariate weather-block bootstrap (stationary climate)."""
import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
import polars as pl

from .heat_pump_weather import bounds, download_weather, validate_weather

UTC = timezone.utc
VARIABLES = ['outdoor_c', 'humidity_pct', 'wind_m_s', 'radiation_kwh_m2']


def load_config(path):
    with open(path, 'rb') as stream:
        c = tomllib.load(stream)
    validate_config(c)
    return c


def validate_config(c):
    bounds(c)
    s = c['sampling']
    reference_config(c)  # validate contiguous reference years too
    for key, lo, hi in [('seed', 0, 2**32-1), ('members', 1, 1000), ('block_days', 1, 90), ('seasonal_window_days', 0, 90)]:
        if type(s[key]) is not int or not lo <= s[key] <= hi:
            raise ValueError(f'sampling.{key} must be an integer in {lo}..{hi}')
    for key in ('max_join_temperature_jump_c', 'heating_degree_base_c', 'cold_day_threshold_c'):
        if not np.isfinite(s[key]):
            raise ValueError(f'sampling.{key} must be finite')
    if s['max_join_temperature_jump_c'] <= 0:
        raise ValueError('max_join_temperature_jump_c must be positive')


def reference_config(config):
    c = deepcopy(config)
    c['weather'].update(years=c['sampling']['reference_years'], start='', end='')
    bounds(c)
    return c


def phase(day):
    """Fixed leap-calendar phase avoids March shifting between leap/nonleap years."""
    return (date(2000, day.month, day.day)-date(2000, 1, 1)).days


def sample_weather(history, config, seed):
    """Copy complete UTC-day blocks, relabel/crop to requested local bounds.

    UTC alignment preserves solar/diurnal timing without stretching source DST days.
    No variable is independently shuffled, interpolated or blended at a join.
    """
    validate_config(config)
    source_start, source_end = bounds(reference_config(config))
    validate_weather(history, source_start, source_end)
    start, end = bounds(config)
    s = config['sampling']
    rng = np.random.default_rng(seed)
    stamps = [datetime.fromisoformat(x) for x in history['interval_start_utc']]
    values = history.select(VARIABLES).to_numpy()
    # Ignore incomplete source UTC days at the two local-boundary edges.
    starts = np.array([i for i, t in enumerate(stamps) if t.hour == 0 and i+24 <= len(stamps)], dtype=int)
    phases = np.array([phase(stamps[i].date()) for i in starts])
    target_day = start.replace(hour=0)
    rows, blocks = [], []
    previous_temperature = None
    zone = ZoneInfo(config['weather']['timezone'])
    block_id = 0
    while target_day < end:
        remaining_days = int(np.ceil((end-target_day).total_seconds()/86400))
        length = min(s['block_days'], remaining_days)
        delta = np.abs(phases-phase(target_day.date()))
        distance = np.minimum(delta, 366-delta)
        eligible = (distance <= s['seasonal_window_days']) & (starts+24*length <= len(stamps))
        if previous_temperature is not None:
            eligible &= np.abs(values[starts, 0]-previous_temperature) <= s['max_join_temperature_jump_c']
        candidates = starts[eligible]
        if not len(candidates):
            raise ValueError(f'No source block for {target_day.date()}; increase seasonal window or join tolerance')
        index = int(rng.choice(candidates))
        jump = None if previous_temperature is None else float(values[index, 0]-previous_temperature)
        blocks.append(dict(block_id=block_id, target_start_utc=target_day.isoformat(), source_start_utc=stamps[index].isoformat(), days=length, candidates=len(candidates), join_temperature_change_c=jump))
        for j in range(24*length):
            target = target_day+timedelta(hours=j)
            if start <= target < end:
                row = dict(interval_start_utc=target.isoformat(), interval_start_local=target.astimezone(zone).isoformat(), source_interval_start_utc=stamps[index+j].isoformat(), block_id=block_id)
                row.update(zip(VARIABLES, map(float, values[index+j])))
                rows.append(row)
        previous_temperature = float(values[index+24*length-1, 0])
        target_day += timedelta(days=length)
        block_id += 1
    frame = pl.DataFrame(rows)
    validate_weather(frame, start, end)
    return frame, blocks


def daily_weather(frame, timezone_name):
    zone = ZoneInfo(timezone_name)
    local_dates = [datetime.fromisoformat(t).astimezone(zone).date().isoformat() for t in frame['interval_start_utc']]
    return frame.with_columns(pl.Series('date', local_dates)).group_by('date').agg(
        pl.col('outdoor_c').mean().alias('mean_c'), pl.col('outdoor_c').min().alias('min_c'),
        pl.col('outdoor_c').max().alias('max_c'), pl.col('humidity_pct').mean(),
        pl.col('wind_m_s').mean(), pl.col('radiation_kwh_m2').sum(), pl.len().alias('hours')).sort('date')


def longest_spell(values, threshold):
    longest = current = 0
    for value in values:
        current = current+1 if value < threshold else 0
        longest = max(longest, current)
    return longest


def summaries(frame, config, label):
    s = config['sampling']
    d = daily_weather(frame, config['weather']['timezone'])
    yearly, monthly = [], []
    for year in sorted({x[:4] for x in d['date']}):
        selected = d.filter(pl.col('date').str.starts_with(year))
        temps = selected['mean_c'].to_numpy()
        hourly = frame.filter(pl.Series([datetime.fromisoformat(t).astimezone(ZoneInfo(config['weather']['timezone'])).year == int(year) for t in frame['interval_start_utc']]))
        t = hourly['outdoor_c'].to_numpy()
        yearly.append(dict(member=label, year=year, days=selected.height, mean_c=float(np.mean(temps)), min_daily_mean_c=float(np.min(temps)), max_daily_mean_c=float(np.max(temps)), heating_degree_days=float(np.maximum(0, s['heating_degree_base_c']-temps).sum()), cold_days=int((temps < s['cold_day_threshold_c']).sum()), longest_cold_spell_days=longest_spell(temps, s['cold_day_threshold_c']), hourly_lag1_correlation=float(np.corrcoef(t[:-1], t[1:])[0, 1]) if np.std(t) > 0 else None))
    for month in range(1, 13):
        selected = d.filter(pl.col('date').str.slice(5, 2) == f'{month:02}')
        if selected.is_empty():
            continue
        temps = selected['mean_c'].to_numpy()
        monthly.append(dict(member=label, month=month, days=selected.height, mean_c=float(temps.mean()), p10_c=float(np.quantile(temps, .1)), p90_c=float(np.quantile(temps, .9))))
    return yearly, monthly


def plot_validation(history_daily, generated_daily, monthly, root):
    fig, axes = plt.subplots(2, 1, figsize=(12, 8))
    for label, color in [('historical', 'black'), ('synthetic', 'tab:orange')]:
        if label == 'historical':
            rows = monthly.filter(pl.col('member') == 'historical').sort('month')
            axes[0].plot(rows['month'], rows['mean_c'], color=color, label='Observed reference pooled monthly mean')
        else:
            rows = monthly.filter(pl.col('member') != 'historical')
            agg = rows.group_by('month').agg(pl.col('mean_c').median().alias('median'), pl.col('mean_c').quantile(.1).alias('p10'), pl.col('mean_c').quantile(.9).alias('p90')).sort('month')
            axes[0].plot(agg['month'], agg['median'], color=color, label='Synthetic member median monthly mean')
            axes[0].fill_between(agg['month'].to_numpy(), agg['p10'].to_numpy(), agg['p90'].to_numpy(), alpha=.25, color=color, label='Synthetic 10–90% member spread')
    axes[0].set(xlabel='Month', ylabel='Temperature (°C)', title='Seasonal temperature check (not a forecast)'); axes[0].legend()
    for frame, label in [(history_daily, 'Observed daily means'), (generated_daily, 'Synthetic daily means')]:
        winter = frame.filter(pl.col('date').str.slice(5, 2).is_in(['12', '01', '02']))['mean_c'].to_numpy()
        ordered = np.sort(winter)
        axes[1].plot(ordered, np.arange(1, len(ordered)+1)/len(ordered), label=label)
    axes[1].set(xlabel='Winter daily mean temperature (°C)', ylabel='Empirical cumulative probability', title='Winter temperature distribution'); axes[1].legend()
    fig.tight_layout(); fig.savefig(root/'weather_validation.png', dpi=150); plt.close(fig)
    first = generated_daily.filter(pl.col('member') == 'member_000')
    fig, ax = plt.subplots(figsize=(13, 4))
    ax.plot([date.fromisoformat(d) for d in first['date']], first['mean_c'], label='Daily mean')
    ax.fill_between([date.fromisoformat(d) for d in first['date']], first['min_c'].to_numpy(), first['max_c'].to_numpy(), alpha=.2, label='Within-day minimum–maximum')
    ax.set(xlabel='Fictional target date', ylabel='Temperature (°C)', title='Synthetic member 000; sampled weather, not a prediction'); ax.legend()
    fig.tight_layout(); fig.savefig(root/'synthetic_daily_temperature.png', dpi=150); plt.close(fig)


def generate(config_path, history_dir, output):
    c = load_config(config_path)
    history_dir, output = Path(history_dir), Path(output)
    if (output.resolve() == history_dir.resolve() or
            output.resolve() in history_dir.resolve().parents or
            history_dir.resolve() in output.resolve().parents):
        raise ValueError('Keep generated weather separate from source history')
    path = history_dir/'weather.txt'
    history = pl.read_csv(path, separator='\t')
    provenance = json.loads((history_dir/'weather.json').read_text())
    if provenance.get('provider') != 'KNMI observations' or provenance['station'] != c['weather']['station'] or provenance['timezone'] != c['weather']['timezone']:
        raise ValueError('Source must be observed KNMI history for the configured station/timezone')
    validate_weather(history, *bounds(reference_config(c)))
    output.mkdir(parents=True, exist_ok=True)
    h_year, h_month = summaries(history, c, 'historical')
    years, months, daily_frames, join_changes = list(h_year), list(h_month), [], []
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    for member in range(c['sampling']['members']):
        label = f'member_{member:03}'
        seed = c['sampling']['seed']+member
        frame, blocks = sample_weather(history, c, seed)
        dest = output/label
        dest.mkdir(exist_ok=True)
        frame.write_csv(dest/'weather.txt', separator='\t')
        pl.DataFrame(blocks).write_csv(dest/'sampled_blocks.txt', separator='\t')
        daily = daily_weather(frame, c['weather']['timezone'])
        daily.write_csv(dest/'daily_weather.txt', separator='\t')
        daily_frames.append(daily.with_columns(pl.lit(label).alias('member')))
        start, end = bounds(c)
        metadata = dict(schema_version=1, provider='Synthetic seasonal block bootstrap', synthetic=True,
                        station=c['weather']['station'], station_name=c['weather']['station_name'], locality=c['weather']['locality'], timezone=c['weather']['timezone'], start_utc=start.isoformat(), end_utc=end.isoformat(), seed=seed,
                        config=c, source_weather_sha256=source_hash, source_provenance=provenance,
                        assumptions=[f"Stationary {min(c['sampling']['reference_years'])}–{max(c['sampling']['reference_years'])} reference climate; no warming adjustment or forecast.", 'Fixed UTC-day multivariate blocks preserve hourly timing and within-block dependence.', 'Seasonal candidate window uses a circular 366-day month/day phase.', 'Temperature-constrained joins alter the bootstrap distribution; other variables can jump.', 'No smoothing, novel extremes, whole-season persistence or guaranteed tail probabilities.'])
        (dest/'weather.json').write_text(json.dumps(metadata, indent=2)+'\n')
        y, m = summaries(frame, c, label)
        years.extend(y); months.extend(m)
        join_changes.extend(abs(b['join_temperature_change_c']) for b in blocks if b['join_temperature_change_c'] is not None)
    annual, monthly = pl.DataFrame(years), pl.DataFrame(months)
    annual.write_csv(output/'annual_weather_checks.txt', separator='\t')
    monthly.write_csv(output/'monthly_weather_checks.txt', separator='\t')
    generated = pl.concat(daily_frames)
    # Quantify seasonal discrepancy rather than declaring statistical validation.
    pooled = monthly.filter(pl.col('member') != 'historical').group_by('month').agg(pl.col('mean_c').mean().alias('synthetic_mean_c'))
    observed = monthly.filter(pl.col('member') == 'historical').select('month', pl.col('mean_c').alias('observed_mean_c'))
    comparison = pooled.join(observed, on='month').with_columns((pl.col('synthetic_mean_c')-pl.col('observed_mean_c')).alias('difference_c')).sort('month')
    comparison.write_csv(output/'seasonal_comparison.txt', separator='\t')
    report = dict(config=c, source_weather_sha256=source_hash, maximum_abs_monthly_mean_difference_c=comparison['difference_c'].abs().max(), maximum_join_temperature_change_c=max(join_changes, default=0), warnings=['Descriptive in-sample checks only; not a calibrated uncertainty distribution.', 'Fixed-length blocks truncate longer weather persistence; inspect cold-spell and degree-day summaries.', 'Hourly joins are constrained, not physical weather transitions.'])
    (output/'sampling_report.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
    plot_validation(daily_weather(history, c['weather']['timezone']), generated, monthly, output)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--history', type=Path, default=Path('data/weather_history_2015_2025'))
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--cache-dir', type=Path, default=Path('data/heat_pump_cache'))
    p.add_argument('--offline', action='store_true', help='Require cached KNMI source responses')
    p.add_argument('--reuse-history', action='store_true', help='Use existing normalized history without network or cache reconstruction')
    a = p.parse_args()
    c = load_config(a.config)
    if not a.reuse_history:
        download_weather(reference_config(c), a.history, a.cache_dir, a.offline)
    report = generate(a.config, a.history, a.output)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
