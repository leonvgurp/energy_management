"""Shared household heatmaps and three-panel daily weather and energy profiles."""
import argparse
import calendar
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import polars as pl
import seaborn as sns

from .household import CONSUMPTION, daily_totals

LABELS = dict(solar_production_kwh='Solar production', heat_pump_consumption_kwh='Space heating',
    boiler_consumption_kwh='Hot-water boiler', phev_consumption_kwh='PHEV charging',
    general_consumption_kwh='General consumption', total_production_kwh='Total production',
    total_consumption_kwh='Total consumption', net_balance_kwh='Net balance (positive = surplus)',
    grid_import_kwh='Grid import', grid_export_kwh='Grid export')
COLORS = dict(zip(CONSUMPTION, sns.color_palette('colorblind', 4)))


def calendar_heatmap(daily, column, cmap='magma', vmin=None, vmax=None):
    """Identical 12×31 layout for any modality; nonexistent dates stay blank."""
    grid = np.full((12, 31), np.nan)
    for stamp, value in daily.select('date', column).iter_rows():
        day = date.fromisoformat(stamp) if isinstance(stamp, str) else stamp
        grid[day.month-1, day.day-1] = value
    fig, ax = plt.subplots(figsize=(15, 5.7))
    sns.heatmap(grid, mask=np.isnan(grid), ax=ax, cmap=cmap,
        vmin=(min(0., np.nanmin(grid)) if vmin is None else vmin), vmax=vmax,
        linewidths=.3, xticklabels=range(1,32), yticklabels=list(calendar.month_abbr)[1:],
        cbar_kws={'label': 'Modeled daily electrical energy (kWh)'})
    ax.set(xlabel='Day of month', ylabel='Month', title=LABELS.get(column, column))
    ax.tick_params(axis='y', rotation=0)
    fig.tight_layout()
    return fig


def select_example_days(hourly, winter_months=(12,1,2), summer_months=(6,7,8)):
    """Select highest/lowest DAILY net energy in each season; stable date ties."""
    daily = daily_totals(hourly)
    rows = []
    for season, months in [('winter', winter_months), ('summer', summer_months)]:
        selected = daily.filter(pl.col('date').str.slice(5,2).cast(pl.Int32).is_in(months)).sort('net_balance_kwh', 'date')
        if selected.is_empty():
            raise ValueError(f'No days available for {season}')
        for label, index in [('worst', 0), ('best', -1)]:
            row = selected.row(index, named=True)
            rows.append(dict(label=f'{season}_{label}', date=row['date'], net_balance_kwh=row['net_balance_kwh']))
    return rows


def day_profile(hourly, day, *, weather, solar_events):
    """Weather above two energy panels; sun height is schematic, without units."""
    if len(day) == 5:
        year = hourly['interval_start_local'][0][:4]
        day = f'{year}-{day}'
    date.fromisoformat(day)
    selected = hourly.filter(pl.col('interval_start_local').str.starts_with(day))
    if selected.is_empty():
        raise ValueError(f'No hourly data for {day}')
    stamps = [datetime.fromisoformat(t) for t in selected['interval_start_utc']]
    # Fixed-offset timezone would mislabel a DST day; derive local label text at
    # each actual UTC tick instead, so repeated clock hours retain their offsets.
    local = [datetime.fromisoformat(t) for t in selected['interval_start_local']]
    # Match the actual modeled temperature by UTC, including repeated DST hours.
    climate = selected.select('interval_start_utc').join(
        weather.select('interval_start_utc', 'outdoor_c'), on='interval_start_utc',
        how='left', validate='1:1', maintain_order='left')
    temperatures = climate['outdoor_c'].to_numpy()
    if not np.isfinite(temperatures).all():
        raise ValueError(f'Missing or nonfinite modeled temperature for {day}')
    events = [r for r in solar_events.iter_rows(named=True) if str(r['date']) == day]
    if len(events) != 1:
        raise ValueError(f'Require exactly one solar event record for {day}')
    event_hours = [events[0][key] for key in ('sunrise_hour', 'solar_noon_hour', 'sunset_hour')]
    if not np.isfinite(event_hours).all() or not 0 <= event_hours[0] < event_hours[1] < event_hours[2] < 24:
        raise ValueError('Require ordered sunrise, solar noon and sunset within the day')
    # Daylight events share an offset in the supported solar model. Select that
    # offset from the day's labels rather than assuming the midnight DST offset.
    daylight = next(t for t in local if t.hour == int(event_hours[1]))
    midnight = datetime.combine(date.fromisoformat(day), time(), daylight.tzinfo)
    sun_stamps = [(midnight+timedelta(hours=float(h))).astimezone(timezone.utc) for h in event_hours]
    fig, panels = plt.subplots(3, 1, figsize=(12, 10), sharex=True)
    weather_ax, *axes = panels
    temp_line, = weather_ax.plot(stamps, temperatures, color='tab:purple', label='Modeled outdoor temperature')
    weather_ax.set_ylabel('Temperature (°C)')
    weather_ax.set_title(f'Weather and daylight · {day}')
    weather_ax.grid(alpha=.2)
    sun_ax = weather_ax.twinx()
    sun_line, = sun_ax.plot([stamps[0], *sun_stamps, stamps[-1]+timedelta(hours=1)],
        [0, 0, 1, 0, 0], color='goldenrod', label='Sunrise → solar noon → sunset (schematic)')
    sun_ax.set_ylim(0, 1.15)
    sun_ax.set_yticks([])
    sun_ax.set_ylabel('Sun profile (no units)')
    weather_ax.legend(handles=[temp_line, sun_line], loc='upper left', fontsize=9)
    for column, label, color in [('total_production_kwh','Production','tab:green'),
        ('total_consumption_kwh','Consumption','tab:red'), ('net_balance_kwh','Net balance','tab:blue')]:
        axes[0].plot(stamps, selected[column], label=label, color=color)
    for column in CONSUMPTION:
        axes[1].plot(stamps, selected[column], label=LABELS[column], color=COLORS[column])
    ticks = list(range(0, len(stamps), 2))
    axes[1].set_xticks([stamps[i] for i in ticks], [local[i].strftime('%H:%M\n%z') for i in ticks])
    for ax in axes:
        ax.axhline(0, color='0.5', lw=.7)
        ax.set_ylabel('Electrical energy (kWh / hour)')
        ax.grid(alpha=.2)
        ax.legend(loc='upper left', ncol=2, fontsize=9)
    axes[0].set_title(f'Modeled household energy · {day} · net {selected["net_balance_kwh"].sum():.1f} kWh')
    axes[1].set_title('Consumption by modality')
    axes[1].set_ylim(bottom=0)
    axes[1].set_xlabel('Local interval start (UTC offset shown)')
    fig.tight_layout()
    return fig


def plot_outputs(hourly, root, config, days=None, *, weather, solar_events):
    root = Path(root)
    daily = daily_totals(hourly)
    for column in LABELS:
        name = column.split('_consumption')[0] if column in CONSUMPTION else ('solar' if column == 'solar_production_kwh' else 'overall')
        dest = root/name
        dest.mkdir(parents=True, exist_ok=True)
        fig = calendar_heatmap(daily, column, config['cmap'])
        fig.savefig(dest/f'{column}_heatmap.png', dpi=150)
        plt.close(fig)
    if days:
        selected = [dict(label=f'selected_{i}', date=day) for i,day in enumerate(days)]
    else:
        selected = select_example_days(hourly, config['winter_months'], config['summer_months'])
    for row in selected:
        fig = day_profile(hourly, row['date'], weather=weather, solar_events=solar_events)
        fig.savefig(root/'overall'/f'{row["label"]}_{row["date"]}.png', dpi=150)
        plt.close(fig)
    (root/'overall'/'selected_days.json').write_text(json.dumps(selected, indent=2)+'\n')
    return selected


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, default=Path('outputs/household'))
    p.add_argument('--day', action='append', help='MM-DD or YYYY-MM-DD, repeatable')
    a = p.parse_args()
    meta = json.loads((a.input/'run.json').read_text())
    hourly = pl.read_csv(a.input/'data/overall/hourly_energy.txt', separator='\t')
    weather = pl.read_csv(a.input/'data/weather/diagnostics.txt', separator='\t')
    events = pl.read_csv(a.input/'data/solar/diagnostics.txt', separator='\t')
    plot_outputs(hourly, a.input/'plots', meta['config']['plots'], a.day,
                 weather=weather, solar_events=events)


if __name__ == '__main__':
    main()
