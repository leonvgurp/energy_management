"""Evening charging with constant or shuffled-cycle daily energy budgets."""
import argparse
import calendar
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import random
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo

import polars as pl

from ..energy_tables import write_energy_tables


def budget_scenarios(c):
    """One full battery before a daily round trip; no intermediate recharge.

    Winter factors multiply battery energy/km. Charging efficiency converts
    battery demand to meter energy once. Petrol demand is not calculated.
    """
    positive = ('usable_battery_kwh', 'reference_range_km', 'round_trip_km')
    nonnegative = ('electricity_eur_kwh', 'ere_eur_kwh')
    for key in positive + nonnegative + ('charging_efficiency', 'ere_eligible_fraction'):
        if not math.isfinite(c[key]):
            raise ValueError(f'{key} must be finite')
    if any(c[k] <= 0 for k in positive) or any(c[k] < 0 for k in nonnegative):
        raise ValueError('Capacity, range and distance must be positive; prices nonnegative')
    if not 0 < c['charging_efficiency'] <= 1 or not 0 <= c['ere_eligible_fraction'] <= 1:
        raise ValueError('Invalid efficiency or eligible fraction')
    factors = c['consumption_factors']
    if not factors or any(not math.isfinite(f) or f <= 0 for f in factors):
        raise ValueError('Consumption factors must be finite and positive')
    rows = []
    for factor in factors:
        battery_per_km = c['usable_battery_kwh'] / c['reference_range_km'] * factor
        available_range = c['usable_battery_kwh'] / battery_per_km
        electric_km = min(c['round_trip_km'], available_range)
        meter = electric_km * battery_per_km / c['charging_efficiency']
        gross = meter * c['electricity_eur_kwh']
        credit = meter * c['ere_eur_kwh'] * c['ere_eligible_fraction']
        rows.append(dict(consumption_factor=factor, electric_range_km=available_range,
                         electric_km=electric_km,
                         distance_beyond_battery_km=c['round_trip_km']-electric_km,
                         battery_kwh=electric_km*battery_per_km,
                         meter_kwh=meter, gross_eur=gross, ere_eur=credit,
                         net_electricity_eur=gross-credit))
    return pl.DataFrame(rows)


def seasonal_factors(c):
    amplitude = c.get('seasonal_amplitude', 0.)
    if not math.isfinite(amplitude) or not 0 <= amplitude < 1:
        raise ValueError('seasonal_amplitude must be in [0, 1)')
    return [1+amplitude*math.cos(2*math.pi*m/12) for m in range(12)]


def monthly_targets(c, year):
    factors = seasonal_factors(c)
    weights = [f*calendar.monthrange(year, m)[1] for m, f in enumerate(factors, 1)]
    targets = c.get('monthly_meter_kwh', [c['annual_meter_kwh']*w/sum(weights) for w in weights])
    if len(targets) != 12 or any(not math.isfinite(t) or t < 0 for t in targets):
        raise ValueError('Require 12 finite nonnegative monthly targets')
    return targets


def daily_budgets(c, year):
    """Shuffle repeated motifs; choose closest monthly budget without rescaling.

    The motif is an assumption informed by coarse observations, not fitted
    weekday dependence. Occasional replacements soften the repeating pattern.
    """
    days = 366 if calendar.isleap(year) else 365
    cap = c.get('max_charge_kwh', 13.)
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError('max_charge_kwh must be finite and positive')
    factors = seasonal_factors(c)
    targets = monthly_targets(c, year)
    if 'daily_cycle_kwh' not in c:
        result = {}
        for i in range(days):
            d = date(year, 1, 1)+timedelta(days=i)
            value = targets[d.month-1]/calendar.monthrange(year,d.month)[1] if c.get('seasonal_amplitude',0) or 'monthly_meter_kwh' in c else c['annual_meter_kwh']/days
            if value > cap:
                raise ValueError('Daily budget exceeds maximum charge')
            result[d] = value
        return result
    motif = c['daily_cycle_kwh']
    if not motif or any(not math.isfinite(x) or x < 0 for x in motif) or not any(motif):
        raise ValueError('Cycle must contain finite nonnegative kWh and positive charging')
    if max(motif) > cap:
        raise ValueError('Cycle charge exceeds max_charge_kwh')
    candidates = c.get('monthly_candidates', 512)
    if not isinstance(candidates, int) or candidates < 1:
        raise ValueError('monthly_candidates must be a positive integer')
    rng = random.Random(c.get('seed', 42))
    result = {}
    for month, target in enumerate(targets, 1):
        n = calendar.monthrange(year, month)[1]
        # Preserve approximate recurring sizes on a half-kWh grid, with a hard meter cap.
        month_motif = [min(cap, round(x*factors[month-1]*2)/2) for x in motif] if c.get('seasonal_amplitude',0) else motif
        best, error = None, math.inf
        for _ in range(candidates):
            values = []
            while len(values) < n:
                block = list(month_motif)
                rng.shuffle(block)
                block = [rng.choice(month_motif) if rng.random() < .2 else x for x in block]
                values.extend(block)
            values = values[:n]
            if 0 in motif and 0 not in values:
                continue
            delta = abs(sum(values)-target)
            if delta < error:
                best, error = values, delta
        if best is None:
            raise ValueError('No valid monthly pattern; increase monthly_candidates')
        result.update({date(year, month, d): value for d, value in enumerate(best, 1)})
    return result


def generate(c, year):
    zone = ZoneInfo(c['timezone'])
    start_time = time.fromisoformat(c['charge_start'])
    if start_time.tzinfo or start_time.second or start_time.microsecond or start_time.hour < 4:
        raise ValueError('Use a local HH:MM start at or after 04:00 to avoid DST ambiguity')
    for key in ('annual_meter_kwh', 'charging_power_kw'):
        if not math.isfinite(c[key]) or c[key] <= 0:
            raise ValueError(f'{key} must be finite and positive')
    first, end = date(year, 1, 1), date(year+1, 1, 1)
    days = (end-first).days
    budgets = daily_budgets(c, year)
    start_hour = start_time.hour+start_time.minute/60
    if max(budgets.values())/c['charging_power_kw'] > 24-start_hour:
        raise ValueError('Daily charge must finish before local midnight')
    cursor = datetime.combine(first, time(), zone).astimezone(timezone.utc)
    stop = datetime.combine(end, time(), zone).astimezone(timezone.utc)
    rows = []
    while cursor < stop:
        local = cursor.astimezone(zone)
        duration = budgets[local.date()]/c['charging_power_kw']
        charge_start = datetime.combine(local.date(), start_time, zone).astimezone(timezone.utc)
        charge_end = charge_start+timedelta(hours=duration)
        overlap = max(0., (min(cursor+timedelta(hours=1), charge_end)-max(cursor, charge_start)).total_seconds()/3600)
        rows.append(dict(interval_start_utc=cursor.isoformat(),
                         interval_start_local=local.isoformat(),
                         energy_kwh=overlap*c['charging_power_kw']))
        cursor += timedelta(hours=1)
    return pl.DataFrame(rows)


def plot_outputs(hourly, daily, c, year, output):
    import matplotlib.pyplot as plt
    import numpy as np
    import seaborn as sns
    sns.set_theme(style='whitegrid')
    dates = daily['date'].to_list()
    indices = {d: i for i, d in enumerate(dates)}
    sums, counts = np.zeros((24, len(dates))), np.zeros((24, len(dates)))
    for r in hourly.iter_rows(named=True):
        t = datetime.fromisoformat(r['interval_start_local'])
        ix = t.hour, indices[t.date().isoformat()]
        sums[ix] += r['energy_kwh']; counts[ix] += 1
    grid = np.divide(sums, counts, out=np.full_like(sums, np.nan), where=counts > 0)
    fig, ax = plt.subplots(figsize=(14, 5))
    sns.heatmap(grid, mask=np.isnan(grid), vmin=0, vmax=c['charging_power_kw'], cmap='magma',
                xticklabels=False, yticklabels=range(24), ax=ax,
                cbar_kws={'label':'Modeled charging energy (kWh/elapsed hour)'})
    ticks = [i for i, d in enumerate(dates) if d.endswith('-01')]
    ax.set_xticks(np.array(ticks)+.5, list(calendar.month_abbr)[1:])
    ax.set(xlabel='Local calendar date', ylabel='Local hour',
           title=f'{year}: modeled evening charging; repeated DST hour averaged for display')
    fig.tight_layout(); fig.savefig(output/f'hourly_heatmap_{year}.png', dpi=150); plt.close(fig)
    daily_grid = np.full((12,31), np.nan)
    for d, energy in daily.iter_rows():
        t = date.fromisoformat(d); daily_grid[t.month-1,t.day-1] = energy
    fig, ax = plt.subplots(figsize=(14, 4))
    sns.heatmap(daily_grid, mask=np.isnan(daily_grid), vmin=0, vmax=c.get('max_charge_kwh',13.), cmap='magma',
                xticklabels=range(1,32), yticklabels=list(calendar.month_abbr)[1:], ax=ax,
                cbar_kws={'label':'Modeled charging (kWh/day)'})
    ax.set(xlabel='Day of month', ylabel='Month', title=f'{year}: modeled evening charging (synthetic pattern)')
    fig.tight_layout(); fig.savefig(output/f'daily_heatmap_{year}.png', dpi=150); plt.close(fig)
    example_date = daily.filter(pl.col('energy_kwh') > 0)['date'][0]
    example = hourly.filter(pl.col('interval_start_local').str.starts_with(example_date))
    example.write_csv(output/'example_day_energy.txt', separator='\t')
    t = time.fromisoformat(c['charge_start'])
    start = t.hour+t.minute/60
    example_kwh = example['energy_kwh'].sum()
    finish = start+example_kwh/c['charging_power_kw']
    fig, ax = plt.subplots(figsize=(10,4))
    ax.plot([0,start,start,finish,finish,24], [0,0,c['charging_power_kw'],c['charging_power_kw'],0,0], label='Assumed charging power')
    ax.set(xlim=(0,24), ylim=(0,c['charging_power_kw']*1.2), xticks=range(0,25,2),
           xlabel='Local hour', ylabel='Charging power (kW)',
           title=f'{example_date}: modeled charge {example_kwh:.2f} kWh; start {c["charge_start"]}')
    ax.legend(); fig.tight_layout(); fig.savefig(output/'example_day_profile.png', dpi=150); plt.close(fig)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True, type=Path)
    p.add_argument('--year', type=int, default=2026)
    p.add_argument('--output', required=True, type=Path)
    a = p.parse_args()
    raw = a.config.read_bytes(); c = tomllib.loads(raw.decode())
    f = generate(c, a.year)
    hourly, daily = write_energy_tables(f, a.output, 'energy_kwh', c['timezone'])
    plot_outputs(hourly, daily, c, a.year, a.output)
    targets = monthly_targets(c, a.year)
    monthly = daily.with_columns(pl.col('date').str.slice(5,2).cast(pl.Int64).alias('month')).group_by('month').agg(
        pl.col('energy_kwh').sum(), (pl.col('energy_kwh') < 1e-9).sum().alias('zero_charge_days')).sort('month').with_columns(
        pl.Series('target_kwh', targets)).with_columns((pl.col('energy_kwh')-pl.col('target_kwh')).alias('difference_kwh'))
    monthly.write_csv(a.output/'monthly_summary.txt', separator='\t')
    (a.output/'run.json').write_text(json.dumps(dict(method='evening_charge_shuffled_cycle_v2' if 'daily_cycle_kwh' in c else 'constant_daily_evening_charge_v1',
        year=a.year, config=c, config_sha256=hashlib.sha256(raw).hexdigest(),
        annual_kwh=hourly['energy_kwh'].sum(),
        assumptions=['Seasonal cosine is a configurable assumption; no fitted weekday effects',
                     'Shuffled cycle is assumed, not a statistically fitted driving cycle',
                     'At most one constant-power charge per day; no taper or standby',
                     'Meter-side energy already includes charging losses',
                     'No solar dispatch or tariff optimization']), indent=2)+'\n')
    print(f'Wrote {hourly.height} hours; {hourly["energy_kwh"].sum():.2f} kWh to {a.output}')


if __name__ == '__main__':
    main()
