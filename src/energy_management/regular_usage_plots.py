"""Explanatory plots and aggregate reports; all calculations use saved model/data."""
import calendar
from datetime import date, datetime
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import polars as pl
import seaborn as sns

from energy_management.regular_usage import COMPONENTS

SEASONS = {'Winter (DJF)': [12, 1, 2], 'Spring (MAM)': [3, 4, 5],
           'Summer (JJA)': [6, 7, 8], 'Autumn (SON)': [9, 10, 11]}


def features(frame):
    local = [datetime.fromisoformat(t) for t in frame['interval_start_local']]
    return frame.with_columns(pl.Series('month', [t.month for t in local]),
                              pl.Series('weekend', [int(t.weekday() >= 5) for t in local]),
                              pl.Series('hour', [t.hour for t in local]))


def seasonal_curves(cells, baseline, shifted, path):
    """Compare equal-household seasonal curves, not individual interval quantiles."""
    b, s = features(baseline), features(shifted)
    annual_mean = b['energy_kwh'].mean()
    fig, axes = plt.subplots(4, 2, figsize=(14, 13), sharex=True, sharey=True)
    for row, (season, months) in enumerate(SEASONS.items()):
        for weekend in (0, 1):
            ax = axes[row, weekend]
            if cells is not None:
                # Held-out households only: keep evaluation independent of calibration.
                selected = cells.filter(pl.col('month').is_in(months) & (pl.col('weekend') == weekend) & (pl.col('split') == 'test'))
                household = selected.group_by('dataset', 'household', 'hour').agg(pl.col('relative_load').mean())
                for dataset, color in [('liander', 'tab:blue'), ('fluvius', 'tab:green')]:
                    q = household.filter(pl.col('dataset') == dataset).group_by('hour').agg(
                        pl.col('relative_load').median().alias('median'),
                        pl.col('relative_load').quantile(.1).alias('p10'),
                        pl.col('relative_load').quantile(.9).alias('p90')).sort('hour')
                    if q.height:
                        ax.plot(q['hour'], q['median']*annual_mean, color=color, label=f'{dataset}: held-out median')
                        ax.fill_between(q['hour'], q['p10']*annual_mean, q['p90']*annual_mean,
                                        color=color, alpha=.12, label=f'{dataset}: household 10–90%')
            for f, label, color, style in [(b, 'Expected baseline', 'black', '-'),
                                           (s, 'Expected shifted', 'tab:orange', '--')]:
                q = f.filter(pl.col('month').is_in(months) & (pl.col('weekend') == weekend)).group_by('hour').agg(pl.col('energy_kwh').mean()).sort('hour')
                ax.plot(q['hour'], q['energy_kwh'], color=color, linestyle=style, label=label)
            ax.set(title=f'{season}: {"weekend" if weekend else "weekday"}', xlabel='Local hour', ylabel='Electricity (kWh/elapsed hour)')
    axes[0, 0].legend(fontsize=7, loc='upper left')
    fig.suptitle('Assumed model vs external household shapes; annual means aligned, spread is not a confidence interval')
    fig.tight_layout(); fig.savefig(path, dpi=140); plt.close(fig)


def plot_outputs(baseline, shifted, bd, sd, samples, root, year, reference=None):
    root = Path(root)
    daily_limit = max(bd['energy_kwh'].max(), sd['energy_kwh'].max())
    for daily, folder, label in [(bd, root/'baseline', 'baseline'), (sd, root, 'shifted')]:
        grid = np.full((12, 31), np.nan)
        for day, energy in daily.iter_rows():
            d = date.fromisoformat(day); grid[d.month-1, d.day-1] = energy
        fig, ax = plt.subplots(figsize=(14, 5))
        sns.heatmap(grid, mask=np.isnan(grid), vmin=0, vmax=daily_limit, cmap='magma', ax=ax,
                    xticklabels=range(1, 32), yticklabels=list(calendar.month_abbr)[1:],
                    cbar_kws={'label': 'Modeled electricity (kWh/day)'})
        ax.set(xlabel='Day of month', ylabel='Month', title=f'{year}: regular electricity, {label} — assumed component budgets')
        fig.tight_layout(); fig.savefig(folder/f'daily_heatmap_{year}.png', dpi=150); plt.close(fig)
    days = bd['date'].to_list(); lookup = {d: i for i, d in enumerate(days)}
    grids = []
    for f in (baseline, shifted):
        sums, counts = np.zeros((24, len(days))), np.zeros((24, len(days)))
        for row in f.iter_rows(named=True):
            local = datetime.fromisoformat(row['interval_start_local'])
            ix = local.hour, lookup[local.date().isoformat()]
            sums[ix] += row['energy_kwh']; counts[ix] += 1
        grids.append(np.divide(sums, counts, out=np.full_like(sums, np.nan), where=counts > 0))
    limit = max(np.nanmax(g) for g in grids)
    for grid, folder, label in zip(grids, [root/'baseline', root], ['baseline', 'shifted']):
        fig, ax = plt.subplots(figsize=(15, 6))
        sns.heatmap(grid, mask=np.isnan(grid), vmin=0, vmax=limit, cmap='magma', ax=ax,
                    xticklabels=False, yticklabels=range(24), cbar_kws={'label': 'Modeled electricity (kWh/elapsed hour)'})
        ticks = [i for i, d in enumerate(days) if d.endswith('-01')]
        ax.set_xticks(np.asarray(ticks)+.5, list(calendar.month_abbr)[1:])
        ax.set(xlabel='Local calendar date', ylabel='Local clock hour',
               title=f'{year}: {label} — assumptions; repeated DST hour averaged only for display')
        fig.tight_layout(); fig.savefig(folder/f'hourly_heatmap_{year}.png', dpi=150); plt.close(fig)
    cells = pl.read_csv(Path(reference)/'household_cells.txt', separator='\t', schema_overrides={'household': pl.String}) if reference else None
    seasonal_curves(cells, baseline, shifted, root/'seasonal_profiles.png')
    b, s = features(baseline), features(shifted)
    fig, axes = plt.subplots(1, 2, figsize=(13, 4), sharey=True)
    for ax, f, label in zip(axes, [b, s], ['Baseline', 'Shifted']):
        q = f.group_by('hour').agg([pl.col(n).mean() for n in COMPONENTS]).sort('hour')
        ax.stackplot(q['hour'], *[q[n] for n in COMPONENTS], labels=COMPONENTS, alpha=.8)
        ax.set(title=label+' annual-average contributions (assumed shares)', xlabel='Local hour', ylabel='Modeled electricity (kWh/elapsed hour)')
    axes[0].legend(fontsize=8); fig.tight_layout(); fig.savefig(root/'component_profiles.png', dpi=150); plt.close(fig)
    fig, ax = plt.subplots(figsize=(14, 4))
    x = np.arange(len(days))
    if samples:
        lo, mid, hi = np.quantile(samples, [.1, .5, .9], axis=0)
        ax.fill_between(x, lo, hi, alpha=.2, label='Variable shifted scenarios: 10–90% (not confidence interval)')
        ax.plot(x, mid, linewidth=.6, alpha=.5, label='Variable scenario median')
    ax.plot(x, bd['energy_kwh'], label='Expected baseline', color='black', linewidth=.8)
    ax.plot(x, sd['energy_kwh'], label='Expected shifted', color='tab:orange', linewidth=.8)
    ax.set(xlabel='Local day index (0 = January 1)', ylabel='Modeled electricity (kWh/day)', title='Paired scenarios preserve annual budgets; variability is assumed')
    ax.legend(fontsize=8); fig.tight_layout(); fig.savefig(root/'daily_scenarios.png', dpi=150); plt.close(fig)
    if reference:
        external = pl.read_csv(Path(reference)/'external_metrics_summary.txt', separator='\t')
        all_model = pl.read_csv(root/'model_metrics.txt', separator='\t')
        model = all_model.filter(pl.col('scenario').is_in(['expected_baseline', 'expected_shifted']))
        comparison = []
        for r in external.filter(pl.col('split') == 'test').iter_rows(named=True):
            for m in model.iter_rows(named=True):
                value = m[r['metric']]
                comparison.append(dict(dataset=r['dataset'], scenario=m['scenario'], metric=r['metric'],
                                       model_value=value, model_p10=value, model_p90=value, samples=1,
                                       external_p10=r['p10'], external_median=r['median'],
                                       external_p90=r['p90'], within_household_10_90=r['p10'] <= value <= r['p90']))
            for scenario in ('variable_baseline', 'variable_shifted'):
                values = all_model.filter(pl.col('scenario') == scenario)[r['metric']]
                if len(values):
                    value = values.median()
                    comparison.append(dict(dataset=r['dataset'], scenario=scenario, metric=r['metric'],
                                           model_value=value, model_p10=values.quantile(.1), model_p90=values.quantile(.9),
                                           samples=len(values), external_p10=r['p10'], external_median=r['median'],
                                           external_p90=r['p90'], within_household_10_90=r['p10'] <= value <= r['p90']))
        pl.DataFrame(comparison).write_csv(root/'diagnostic_comparison.txt', separator='\t')
        monthly = pl.read_csv(Path(reference)/'household_monthly.txt', separator='\t', schema_overrides={'household': pl.String})
        fig, ax = plt.subplots(figsize=(12, 4))
        for dataset, color in [('liander', 'tab:blue'), ('fluvius', 'tab:green')]:
            q = monthly.filter((pl.col('dataset') == dataset) & (pl.col('split') == 'test')).group_by('month').agg(
                pl.col('annual_share').median().alias('median'), pl.col('annual_share').quantile(.1).alias('p10'), pl.col('annual_share').quantile(.9).alias('p90')).sort('month')
            ax.plot(q['month'], q['median']*100, label=dataset+' held-out median', color=color)
            ax.fill_between(q['month'], q['p10']*100, q['p90']*100, alpha=.12, color=color)
        for f, label in [(b, 'Baseline'), (s, 'Shifted')]:
            q = f.group_by('month').agg(pl.col('energy_kwh').sum()).sort('month')
            ax.plot(q['month'], q['energy_kwh']/f['energy_kwh'].sum()*100, label=label)
        ax.set(xlabel='Month', ylabel='Annual electricity (%)', title='Monthly energy shares; external household 10–90% spread')
        ax.legend(); fig.tight_layout(); fig.savefig(root/'monthly_comparison.png', dpi=150); plt.close(fig)


def reference_report(cells, monthly, variability, config, evaluation, root):
    root = Path(root)
    from energy_management.regular_usage import generate_pair
    b, s = generate_pair(config, 2026)
    seasonal_curves(cells, b, s, root/'external_seasonal_profiles.png')
    rows = []
    for dataset in cells['dataset'].unique().sort():
        for split in ('train', 'test'):
            selected = cells.filter((pl.col('dataset') == dataset) & (pl.col('split') == split))
            for household in selected['household'].unique().sort():
                h = selected.filter(pl.col('household') == household)
                january = h.filter(pl.col('month') == 1)['relative_load'].mean()
                july = h.filter(pl.col('month') == 7)['relative_load'].mean()
                hourly = h.group_by('hour').agg(pl.col('relative_load').mean()).sort('hour')
                morning = hourly.filter(pl.col('hour').is_between(5, 11))
                evening = hourly.filter(pl.col('hour').is_between(16, 23))
                rows.append(dict(dataset=dataset, split=split, household=household,
                                 january_july_ratio=january/july,
                                 morning_peak_hour=int(morning['hour'][morning['relative_load'].arg_max()]),
                                 evening_peak_hour=int(evening['hour'][evening['relative_load'].arg_max()]),
                                 evening_share=float(hourly.filter(pl.col('hour') >= 17)['relative_load'].sum()/hourly['relative_load'].sum()),
                                 weekend_weekday_ratio=float(h.filter(pl.col('weekend') == 1)['relative_load'].mean()/h.filter(pl.col('weekend') == 0)['relative_load'].mean())))
    metrics = pl.DataFrame(rows).join(variability, on=['dataset', 'split', 'household'])
    metrics.write_csv(root/'household_metrics.txt', separator='\t')
    aggregates = []
    for dataset in ['liander', 'fluvius']:
        for split in ['train', 'test']:
            f = metrics.filter((pl.col('dataset') == dataset)&(pl.col('split') == split))
            for metric in ['january_july_ratio', 'morning_peak_hour', 'evening_peak_hour', 'evening_share', 'weekend_weekday_ratio', 'daily_cv', 'hour_p10', 'hour_p90']:
                aggregates.append(dict(dataset=dataset, split=split, metric=metric, households=f.height,
                                       mean=f[metric].mean(), median=f[metric].median(),
                                       p10=f[metric].quantile(.1), p90=f[metric].quantile(.9)))
    aggregate = pl.DataFrame(aggregates)
    aggregate.write_csv(root/'external_metrics_summary.txt', separator='\t')
    lines = ['# Regular electricity reference evidence', '',
             'Generated from downloaded household readings by `pixi run regular-usage-reference`.',
             'Each household is normalized by annual mean hourly consumption and weighted equally.',
             'Household spreads describe heterogeneity, not confidence intervals or this home.', '',
             '| Dataset | Split | Households | January/July median (10–90%) |',
             '| --- | --- | ---: | --- |']
    for r in aggregate.filter(pl.col('metric') == 'january_july_ratio').iter_rows(named=True):
        lines.append(f'| {r["dataset"]} | {r["split"]} | {r["households"]} | {r["median"]:.3f} ({r["p10"]:.3f}–{r["p90"]:.3f}) |')
    lines += ['', 'Calibration fits eight overall shape parameters on Fluvius training households only.',
              'Component shares and movement fractions remain user assumptions.', '',
              '| Dataset | Split | Model | Relative-load cell RMSE |', '| --- | --- | --- | ---: |']
    for r in evaluation.iter_rows(named=True):
        lines.append(f'| {r["dataset"]} | {r["split"]} | {r["model"]} | {r["cell_rmse"]:.4f} |')
    import json
    calibration = json.loads((root/'calibration.json').read_text())
    lines += ['', 'Shape parameters at fitted bounds: '+', '.join(calibration['parameters_at_bounds'])+'.',
              'Bound hits indicate restricted templates/assumed shares; fitted peak parameters are not measured behaviour.',
              'Evaluation is exploratory: example settings were refined during development; this is not an untouched prospective test.',
              '', 'RMSE uses 576 equally weighted monthly × day-type × clock-hour cells.',
              'Liander technologies are unknown; exporters are excluded, but remaining PV/EV/heating cannot be ruled out.',
              'Liander uses a nominal clock grid; Fluvius labels have a misleading Z suffix but follow local DST.',
              'Both sources omit DST days from clock-hour comparisons. Annual energy remains intact.',
              'Fluvius absence indicators do not guarantee equipment absence.',
              'Source hashes, URLs and quality exclusions are saved beside this report.', '']
    (root/'reference_report.md').write_text('\n'.join(lines))


def model_metrics(frame):
    """Dimensionless diagnostics comparable with normalized household summaries."""
    f = features(frame)
    energy = f['energy_kwh'].to_numpy()
    mean = energy.mean()
    if mean == 0:
        return {key: 0. for key in ('january_july_ratio', 'morning_peak_hour', 'evening_peak_hour',
                                    'evening_share', 'weekend_weekday_ratio', 'daily_cv', 'hour_p10', 'hour_p90')}
    # Same equal-month/day-type cell averaging as the external metric summary.
    cells = f.group_by('month','weekend','hour').agg(pl.col('energy_kwh').mean())
    h = cells.group_by('hour').agg(pl.col('energy_kwh').mean()).sort('hour')
    morning = h.filter(pl.col('hour').is_between(5,11))
    evening = h.filter(pl.col('hour').is_between(16,23))
    daily = f.with_columns(pl.col('interval_start_local').str.slice(0,10).alias('date')).group_by('date').agg(
        pl.col('energy_kwh').sum().alias('kwh'),pl.len().alias('hours')).filter(pl.col('hours')==24)['kwh']
    normal = f.with_columns(pl.col('interval_start_local').str.slice(0,10).alias('date'))
    valid_dates = normal.group_by('date').len().filter(pl.col('len')==24)['date']
    values = normal.filter(pl.col('date').is_in(valid_dates.implode()))['energy_kwh']/mean
    return dict(january_july_ratio=float(cells.filter(pl.col('month')==1)['energy_kwh'].mean()/cells.filter(pl.col('month')==7)['energy_kwh'].mean()),
                morning_peak_hour=int(morning['hour'][morning['energy_kwh'].arg_max()]),
                evening_peak_hour=int(evening['hour'][evening['energy_kwh'].arg_max()]),
                evening_share=float(h.filter(pl.col('hour')>=17)['energy_kwh'].sum()/h['energy_kwh'].sum()),
                weekend_weekday_ratio=float(cells.filter(pl.col('weekend')==1)['energy_kwh'].mean()/cells.filter(pl.col('weekend')==0)['energy_kwh'].mean()),
                daily_cv=float(daily.std()/daily.mean()),hour_p10=float(values.quantile(.1)),hour_p90=float(values.quantile(.9)))
