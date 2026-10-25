"""Offline explanatory plots from saved heat-pump scenario outputs."""
import argparse
import calendar
import json
from datetime import date, datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import polars as pl
import seaborn as sns


def plot_saved_outputs(root):
    root = Path(root)
    metadata = json.loads((root/'run.json').read_text()) if (root/'run.json').exists() else {}
    synthetic = metadata.get('weather', {}).get('synthetic', False)
    weather_label = 'synthetic weather' if synthetic else 'observed weather'
    colors = dict(low='tab:blue', base='tab:orange', high='tab:red')
    frames = {s: pl.read_csv(root/s/'diagnostics.txt', separator='\t') for s in colors}
    base = frames['base']
    years = sorted({int(d[:4]) for d in base['date']})
    daily = base.group_by('date').agg(pl.col('consumption_kwh').sum().alias('kwh'), pl.col('outdoor_c').mean().alias('temperature')).sort('date')
    limit = max(1, daily['kwh'].max())
    for year in years:
        grid = np.full((12, 31), np.nan)
        for r in daily.iter_rows(named=True):
            d = date.fromisoformat(r['date'])
            if d.year == year:
                grid[d.month-1, d.day-1] = r['kwh']
        fig, ax = plt.subplots(figsize=(14, 5))
        sns.heatmap(grid, mask=np.isnan(grid), vmin=0, vmax=limit, cmap='magma', ax=ax,
                    xticklabels=range(1, 32), yticklabels=list(calendar.month_abbr)[1:], cbar_kws={'label': 'Modeled electricity (kWh/day)'})
        ax.set(xlabel='Day of month', ylabel='Month', title=f'{year}: base heating scenario, {weather_label} (assumed parameters)')
        fig.tight_layout(); fig.savefig(root/f'daily_heatmap_{year}.png', dpi=150); plt.close(fig)
    # Complete DJF local days only, pooled across requested weather years.
    counts = base.group_by('date').len()
    winter = daily.join(counts, on='date').filter(pl.col('date').str.slice(5, 2).is_in(['12', '01', '02']) & (pl.col('len') == 24))
    if winter.height >= 4:
        quantiles = np.quantile(winter['temperature'].to_numpy(), [.1, .35, .65, .9])
        available = winter.to_dicts()
        chosen = []
        for q in quantiles:
            r = min(available, key=lambda r: (abs(r['temperature']-q), r['date']))
            chosen.append(r); available.remove(r)
        fig, axes = plt.subplots(2, 2, figsize=(13, 8), sharex=True)
        for ax, day in zip(axes.flat, chosen):
            for s, frame in frames.items():
                rows = frame.filter(pl.col('date') == day['date'])
                hours = [datetime.fromisoformat(t).hour for t in rows['interval_start_local']]
                ax.plot(hours, rows['consumption_kwh'], label=s, color=colors[s])
            rows = base.filter(pl.col('date') == day['date'])
            twin = ax.twinx()
            twin.plot(hours, rows['outdoor_c'], '--', color='gray', label=f'Outdoor temperature ({weather_label})')
            twin.set_ylabel('Outdoor temperature (°C)')
            ax.set(title=day['date'], xlabel='Local hour', ylabel='Modeled electricity (kWh/hour)')
            ax.legend(loc='upper left')
        fig.suptitle(f'Representative winter days, {weather_label}; consumption is modeled')
        fig.tight_layout(); fig.savefig(root/'winter_days.png', dpi=150); plt.close(fig)
        edges = np.quantile(winter['temperature'].to_numpy(), [.25, .5, .75])
        lookup = {r['date']: int(np.searchsorted(edges, r['temperature'])) for r in winter.iter_rows(named=True)}
        groups = {(g, hour): [] for g in range(4) for hour in range(24)}
        for r in base.iter_rows(named=True):
            if r['date'] in lookup:
                groups[lookup[r['date']], datetime.fromisoformat(r['interval_start_local']).hour].append(r['consumption_kwh'])
        fig, axes = plt.subplots(2, 2, figsize=(12, 7), sharex=True, sharey=True)
        for g, ax in enumerate(axes.flat):
            med, lower, upper = [], [], []
            for hour in range(24):
                values = groups[g, hour]
                q = np.quantile(values, [.1, .5, .9]) if values else [np.nan]*3
                lower.append(q[0]); med.append(q[1]); upper.append(q[2])
            ax.plot(range(24), med)
            ax.fill_between(range(24), lower, upper, alpha=.2)
            count = sum(v == g for v in lookup.values())
            ax.set(title=f'Temperature quartile {g+1} (cold → mild), n={count} days', xlabel='Local hour', ylabel='Modeled electricity (kWh/hour)')
        fig.suptitle(f'Base model, {weather_label}: median and 10–90% day spread; not confidence intervals')
        fig.tight_layout(); fig.savefig(root/'winter_profiles.png', dpi=150); plt.close(fig)
    monthly = {}
    for s, frame in frames.items():
        monthly[s] = frame.with_columns(pl.col('date').str.slice(0, 7).alias('month')).group_by('month').agg(pl.col('consumption_kwh').sum()).sort('month')
    fig, ax = plt.subplots(figsize=(14, 5))
    for s, frame in monthly.items():
        ax.plot(frame['month'].to_list(), frame['consumption_kwh'], label=s, color=colors[s])
    ax.set(xlabel='Local calendar month', ylabel='Modeled electricity (kWh/month)', title=f'Bundled assumption sensitivity, {weather_label}; no fitted totals')
    ax.tick_params(axis='x', rotation=60); ax.legend()
    fig.tight_layout(); fig.savefig(root/'scenario_comparison.png', dpi=150); plt.close(fig)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', '--output', dest='input', type=Path, required=True, help='Existing model output directory')
    a = p.parse_args()
    plot_saved_outputs(a.input)


if __name__ == '__main__':
    main()
