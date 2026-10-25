"""Simple hot-water-equivalent energy replacement, independent of space heating."""
import argparse
from collections import deque
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo

import polars as pl
from .energy_tables import hourly_energy, write_energy_tables

# Water density approximated as 1 kg/l; specific heat 4.186 kJ/(kg K).
KWH_PER_L_K = 4.186 / 3600
SOURCE = 'https://professional.vaillant.co.uk/downloads/aproducts/renewables-1/arostor-1/0020285063-00-arostor-200-270ltr-install-1456700.pdf'


def validate(c):
    ZoneInfo(c['timezone'])
    for key in ('volume_l', 'setpoint_c', 'cold_water_c', 'heat_pump_limit_c',
                'cop', 'heat_pump_thermal_kw', 'resistance_kw', 'standing_electric_kw'):
        if not math.isfinite(c[key]):
            raise ValueError(f'{key} must be finite')
    if not c['cold_water_c'] < c['heat_pump_limit_c'] <= c['setpoint_c'] <= 65:
        raise ValueError('Require cold < heat pump limit <= setpoint <= 65 C')
    if any(c[k] <= 0 for k in ('volume_l', 'cop', 'heat_pump_thermal_kw', 'resistance_kw')) or c['standing_electric_kw'] < 0:
        raise ValueError('Powers, COP and volume must be positive; standing allowance nonnegative')
    if not c['draws']:
        raise ValueError('At least one daily draw is required')
    seen = set()
    for draw in c['draws']:
        t = time.fromisoformat(draw['time'])
        if t.tzinfo or t.second or t.microsecond or t.minute not in (0, 30) or 1 <= t.hour <= 3:
            raise ValueError('Draws must use half-hour local times outside 01:00–03:59 DST ambiguity window')
        if t in seen or not math.isfinite(draw['fraction']) or not 0 < draw['fraction'] <= 1:
            raise ValueError('Draw times must be unique and fractions in (0, 1]')
        seen.add(t)


def draw_energy(c, fraction):
    """Return heat-pump and resistance thermal kWh for hot-equivalent volume."""
    v = c['volume_l'] * fraction * KWH_PER_L_K
    return v * (c['heat_pump_limit_c'] - c['cold_water_c']), v * (c['setpoint_c'] - c['heat_pump_limit_c'])


def simulate(c, start, end):
    """Half-hour UTC grid; sequential recovery jobs carry across midnight.

    Start with no outstanding recovery at local midnight. Demand is an energy
    budget, not a stratified tank or a prediction of delivered tap temperature.
    """
    validate(c)
    if end <= start:
        raise ValueError('End must be after start')
    zone = ZoneInfo(c['timezone'])
    cursor = datetime.combine(start, time(), zone).astimezone(timezone.utc)
    stop = datetime.combine(end, time(), zone).astimezone(timezone.utc)
    draws = {time.fromisoformat(d['time']): d['fraction'] for d in c['draws']}
    jobs, rows = deque(), []
    while cursor < stop:
        local = cursor.astimezone(zone)
        fraction = draws.get(local.time(), 0)
        hp, element = draw_energy(c, fraction)
        requested = hp + element
        if fraction:
            jobs.append([hp, element])
        remaining_hours, hp_heat, element_heat = 0.5, 0.0, 0.0
        while jobs and remaining_hours > 1e-12:
            job = jobs[0]
            stage = 0 if job[0] > 1e-12 else 1
            power = c['heat_pump_thermal_kw'] if stage == 0 else c['resistance_kw']
            heat = min(job[stage], power * remaining_hours)
            job[stage] -= heat
            remaining_hours -= heat / power
            if stage == 0:
                hp_heat += heat
            else:
                element_heat += heat
            if sum(job) <= 1e-12:
                jobs.popleft()
        hp_electric = hp_heat / c['cop']
        standing = c['standing_electric_kw'] * 0.5
        rows.append(dict(interval_start_utc=cursor.isoformat(), interval_start_local=local.isoformat(),
                         date=local.date().isoformat(), draw_heat_kwh=requested,
                         heat_pump_heat_kwh=hp_heat, resistance_heat_kwh=element_heat,
                         heat_pump_electric_kwh=hp_electric, resistance_electric_kwh=element_heat,
                         standing_electric_kwh=standing, consumption_kwh=hp_electric+element_heat+standing,
                         outstanding_heat_kwh=sum(sum(j) for j in jobs)))
        cursor += timedelta(minutes=30)
    return pl.DataFrame(rows)


def hourly_diagnostics(frame, timezone_name):
    """Sum interval energies; retain recovery debt at each hour's end."""
    labels = hourly_energy(frame, 'consumption_kwh', timezone_name, 30)
    keys = [datetime.fromisoformat(t).replace(minute=0).isoformat()
            for t in frame['interval_start_utc']]
    energies = [name for name in frame.columns
                if name.endswith('_kwh') and name != 'outstanding_heat_kwh']
    grouped = frame.with_columns(pl.Series('hour', keys)).group_by('hour', maintain_order=True).agg(
        pl.col('date').first(), *[pl.col(name).sum() for name in energies],
        pl.col('outstanding_heat_kwh').last())
    return labels.select('interval_start_utc', 'interval_start_local').hstack(
        grouped.drop('hour')).select(frame.columns)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--start', type=date.fromisoformat, required=True)
    p.add_argument('--end', type=date.fromisoformat, required=True, help='Exclusive local date')
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    raw = a.config.read_bytes()
    c = tomllib.loads(raw.decode())
    frame = hourly_diagnostics(simulate(c, a.start, a.end), c['timezone'])
    a.output.mkdir(parents=True, exist_ok=True)
    frame.write_csv(a.output/'diagnostics.txt', separator='\t')
    hourly, daily = write_energy_tables(frame, a.output, 'consumption_kwh', c['timezone'])
    hourly.rename({'energy_kwh': 'consumption_kwh'}).write_csv(a.output/'hourly_consumption.txt', separator='\t')
    daily = daily.rename({'energy_kwh': 'daily_kwh'})
    daily.write_csv(a.output/'daily_consumption.txt', separator='\t')
    # A single representative day; hourly energy remains in the saved tables.
    import matplotlib.pyplot as plt
    import seaborn as sns
    sns.set_theme(style='whitegrid')
    first = frame.filter(pl.col('date') == str(a.start))
    hours = [datetime.fromisoformat(t).hour + datetime.fromisoformat(t).minute/60
             for t in first['interval_start_local']]
    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.bar(range(first.height), first['consumption_kwh'].to_numpy(), width=0.95, align='edge')
    ax.set_xticks(range(first.height)[::3], [f'{h:02.0f}:00' for h in hours[::3]])
    ax.set(xlabel='Local interval start (elapsed hours; repeated hour on autumn DST day)',
           ylabel='Average electrical power (kW)',
           title=f'Illustrative aroSTOR electricity: {a.start}, COP {c["cop"]}')
    fig.tight_layout()
    fig.savefig(a.output/'daily_profile.png', dpi=160)
    plt.close(fig)
    summary = dict(consumption_kwh=frame['consumption_kwh'].sum(), outstanding_heat_kwh=frame['outstanding_heat_kwh'][-1])
    (a.output/'run.json').write_text(json.dumps(dict(method='hot-equivalent replacement budget; sequential heating', config=c,
        output_interval_minutes=60, calculation_step_minutes=30,
        config_sha256=hashlib.sha256(raw).hexdigest(), start=str(a.start), end=str(a.end), source=SOURCE,
        retrieved='2026-10-25', initial_state='No outstanding recovery', summary=summary), indent=2)+'\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
