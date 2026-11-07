"""Paired annual policy analysis using existing weather/component models.

Monthly load costs use proportional sharing of hourly grid imports. This is an
accounting allocation, not a marginal saving from removing an appliance.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import polars as pl
import seaborn as sns

from energy_management.config import load_config
from energy_management.costs import comparison, monthly_costs
from energy_management.ensemble import RunningMean, bill_metrics, energy_metrics, record, statistics
from energy_management.household import ENERGY_COLUMNS, simulate
from energy_management.weather_scenarios import load_config as load_weather_config, sample_weather

LOADS = ['heat_pump', 'boiler', 'phev', 'general']
LABELS = ['Space heating', 'Hot water', 'Car charging', 'General use']
COLORS = ['#4477aa', '#ee9944', '#aa3377', '#66aa88']
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def write(frame, path):
    frame.write_csv(path, separator='\t')


def run(args):
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    sampling = load_weather_config(args.weather_config)
    source = args.history/'weather.txt'
    source_metadata = json.loads((args.history/'weather.json').read_text())
    assert source_metadata['provider'] == 'KNMI observations'
    assert source_metadata['station'] == sampling['weather']['station']
    assert source_metadata['timezone'] == config['scenario']['timezone']
    history = pl.read_csv(source, separator='\t')
    year, = sampling['weather']['years']
    n = sampling['sampling']['members']
    start_seed = sampling['sampling']['seed']
    annual_rows, monthly_rows, blocks_rows = [], [], []
    moments = RunningMean(ENERGY_COLUMNS)
    for index in range(n):
        seed = start_seed + index
        weather, blocks = sample_weather(history, sampling, seed)
        weather_path = out/'weather'/f'member_{index:03d}.txt'
        weather_path.parent.mkdir(exist_ok=True)
        write(weather, weather_path)
        blocks_rows.extend(dict(member=index, **block) for block in blocks)
        c = deepcopy(config)
        c['scenario']['seed'] = seed
        c['weather'].update(mode='file', file=str(weather_path))
        h, _, diagnostics, _ = simulate(c, year, args.config.resolve().parent)
        moments.add(h.select('interval_start_utc', 'interval_start_local', *ENERGY_COLUMNS))
        bills = comparison(h, c['costs'], c['scenario']['timezone'])
        values = {**energy_metrics(h), **bill_metrics(bills)}
        p, r = c['costs']['import_single_eur_kwh'], c['costs']['export_eur_kwh']
        assert c['costs']['meter'] == 'single', 'Attribution requires a single-rate contract'
        assert len(c['costs']['export_charges']) == 1
        fee = c['costs']['export_charges'][0]
        assert fee['basis'] == 'gross_export' and not fee['tiers']
        assert fee['eur_day'] == fee['eur_year'] == 0
        f = fee['eur_kwh']
        # Assign the annual policy benefit to the exports that generated it.
        net_fraction = bills['with_saldering']['netted_kwh']/values['grid_export_kwh'] if values['grid_export_kwh'] else 0
        assert np.isclose(values['without_minus_with_saldering_eur'], (p-r)*bills['with_saldering']['netted_kwh'])
        record(annual_rows, values, run=index, seed=seed, variant='scenario')
        month_ids = h['interval_start_local'].str.slice(5, 2).cast(pl.Int32).to_numpy()
        settled = {regime: monthly_costs(h, c['costs'], c['scenario']['timezone'], enabled)
                   for regime, enabled in [('with_saldering', True), ('without_saldering', False)]}
        for month in range(1, 13):
            mask = month_ids == month
            row = {key: float(h[key].to_numpy()[mask].sum()) for key in ENERGY_COLUMNS}
            for regime, rows in settled.items():
                row[f'{regime}_total_eur'] = rows[month-1]['total_eur']
            row['export_origin_saldering_benefit_eur'] = row['grid_export_kwh']*net_fraction*(p-r)
            row['solar_direct_value_eur'] = row['self_consumption_kwh']*p
            row['solar_export_value_without_eur'] = row['grid_export_kwh']*(r-f)
            row['solar_export_value_with_eur'] = row['grid_export_kwh']*(net_fraction*p+(1-net_fraction)*r-f)
            consumption = h['total_consumption_kwh'].to_numpy()[mask]
            imports = h['grid_import_kwh'].to_numpy()[mask]
            for load in LOADS:
                share = np.divide(h[f'{load}_consumption_kwh'].to_numpy()[mask], consumption,
                                  out=np.zeros_like(consumption), where=consumption > 0)
                row[f'{load}_gross_import_eur'] = float((share*imports*p).sum())
            assert np.isclose(sum(row[f'{load}_gross_import_eur'] for load in LOADS), row['grid_import_kwh']*p)
            record(monthly_rows, row, run=index, seed=seed, variant='scenario', month=month)
        # Retain model quality diagnostics, not just favorable cost outcomes.
        if index == 0:
            for name in ('heat_pump', 'boiler'):
                write(diagnostics[name], out/f'first_member_{name}_diagnostics.txt')
        print(f'{index+1}/{n} bootstrapped household years settled', flush=True)
    annual, monthly = pl.DataFrame(annual_rows), pl.DataFrame(monthly_rows)
    a = statistics(annual, ['variant', 'metric', 'unit'])
    m = statistics(monthly, ['variant', 'month', 'metric', 'unit'])
    for name, frame in [('annual_runs', annual), ('annual_summary', a), ('monthly_runs', monthly),
                        ('monthly_summary', m), ('weather_blocks', pl.DataFrame(blocks_rows)),
                        ('hourly_energy_mean', moments.frame()), ('hourly_energy_std', moments.std_frame())]:
        write(frame, out/f'{name}.txt')
    for name, path in [('scenario.toml', args.config), ('weather_sampling.toml', args.weather_config)]:
        (out/name).write_bytes(path.read_bytes())
    inputs = [args.config, args.weather_config, source, args.history/'weather.json', Path(__file__)]
    inputs += list(Path('src/energy_management').rglob('*.py'))
    meta = dict(members=n, seeds=list(range(start_seed, start_seed+n)), calendar_year=year,
                source_metadata=source_metadata,
                sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs})
    (out/'provenance.json').write_text(json.dumps(meta, indent=2)+'\n')
    plots(a, m, annual, out)
    report(a, m, config, out, n)


def plots(a, m, runs, out):
    sns.set_theme(style='whitegrid', context='notebook')
    def monthly(key):
        return m.filter(pl.col('metric') == key).sort('month')['mean'].to_numpy()
    def annual(key):
        return a.filter(pl.col('metric') == key)['mean'][0]
    x = np.arange(12)
    fig, axes = plt.subplots(2, 1, figsize=(12, 9), sharex=True)
    base = np.zeros(12)
    for load, label, color in zip(LOADS, LABELS, COLORS):
        y = monthly(f'{load}_consumption_kwh')
        axes[0].bar(x, y, bottom=base, color=color, label=label)
        base += y
    axes[0].plot(x, monthly('solar_production_kwh'), 'o-', color='#ccbb22', lw=3, label='Solar yield')
    axes[0].set(ylabel='Electrical energy (kWh/month)', title='Winter heating demand; summer solar surplus')
    axes[0].legend(ncol=3, fontsize=9)
    for key, label, color in [('grid_import_kwh', 'Grid import', '#bb5566'), ('grid_export_kwh', 'Grid export', '#228833'),
                               ('self_consumption_kwh', 'Solar used directly', '#ccbb22')]:
        axes[1].plot(x, monthly(key), 'o-', label=label, color=color)
    axes[1].set(ylabel='Energy (kWh/month)', xticks=x, xticklabels=MONTHS)
    axes[1].legend()
    fig.suptitle('Mean of 100 paired bootstrapped weather years · fixed monthly PV targets')
    fig.tight_layout(); fig.savefig(out/'seasonal_energy.png', dpi=170); plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(13, 5), gridspec_kw={'width_ratios': [1.2, 1]})
    names = ['Grid imports', 'Netting credit', 'Export receipt', 'Export fee', 'Fixed net', 'TOTAL']
    line_names = ['grid_import', 'saldering_credit', 'export_remuneration']
    for offset, regime, label, color in [(-.18, 'with_saldering', 'With saldering', '#228833'),
                                          (.18, 'without_saldering', 'Without saldering', '#bb5566')]:
        amounts = [annual(f'{regime}_line_{i}_{name}_eur') for i, name in enumerate(line_names)]
        amounts += [annual(f'{regime}_line_6_Terugleverkosten_eur'),
                    sum(annual(f'{regime}_line_{i}_{name}_eur') for i, name in
                        [(3, 'Vaste leveringskosten'), (4, 'Systeembeheerkosten'), (5, 'Vermindering energiebelasting')]),
                    annual(f'{regime}_total_eur')]
        bars = axes[0].bar(np.arange(6)+offset, amounts, .36, label=label, color=color)
        axes[0].bar_label(bars, fmt='%.0f', fontsize=8, padding=3)
        vals = runs.filter(pl.col('metric') == f'{regime}_total_eur')['value']
        axes[1].hist(vals, bins=15, alpha=.6, label=label, color=color)
    axes[0].axhline(0, color='black', lw=.8)
    axes[0].set(xticks=range(6), xticklabels=names, ylabel='EUR/year', title='Why the annual bill has this value')
    axes[0].tick_params(axis='x', rotation=25)
    axes[1].set(xlabel='Annual electricity bill (EUR)', ylabel='Simulated years', title='Weather and timing spread')
    for ax in axes: ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig(out/'annual_bill.png', dpi=170); plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(12, 9), sharex=True)
    for regime, label, color in [('with_saldering', 'With saldering', '#228833'), ('without_saldering', 'Without saldering', '#bb5566')]:
        rows = m.filter(pl.col('metric') == f'{regime}_total_eur').sort('month')
        axes[0].plot(x, rows['mean'], 'o-', color=color, label=label)
        axes[0].fill_between(x, rows['p05'], rows['p95'], color=color, alpha=.15)
    axes[0].set(ylabel='Allocated annual bill (EUR/month)', title='Costs concentrate in the heating season · bands: simulated 5–95%')
    axes[0].legend()
    axes[1].bar(x, monthly('export_origin_saldering_benefit_eur'), color='#4477aa')
    axes[1].set(ylabel='Annual policy benefit generated (EUR)', title='Summer exports create most of the annual saldering benefit', xticks=x, xticklabels=MONTHS)
    fig.text(.5, .01, 'Top: annual credit allocated across import months. Bottom: benefit attributed to export months. Neither is a monthly invoice.', ha='center', fontsize=9)
    fig.tight_layout(rect=[0,.03,1,1]); fig.savefig(out/'seasonal_cost_and_saldering.png', dpi=170); plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(12, 9), sharex=True)
    base = np.zeros(12)
    for load, label, color in zip(LOADS, LABELS, COLORS):
        y = monthly(f'{load}_gross_import_eur')
        axes[0].bar(x, y, bottom=base, label=label, color=color)
        base += y
    axes[0].set(ylabel='Gross import cost (EUR/month)', title='Loads behind the import bill · shared hourly imports allocated in proportion to demand')
    axes[0].legend(ncol=4, fontsize=9)
    base = np.zeros(12)
    for key, label, color in [('solar_direct_value_eur', 'Direct solar: avoided imports', '#ccbb22'),
                              ('solar_export_value_without_eur', 'Exports after fee, no saldering', '#66aa88'),
                              ('export_origin_saldering_benefit_eur', 'Additional value from saldering', '#4477aa')]:
        y = monthly(key)
        axes[1].bar(x, y, bottom=base, label=label, color=color)
        base += y
    axes[1].set(ylabel='Solar operating value (EUR/month)', title='Direct solar is valuable; exports lose most of their value to the export fee', xticks=x, xticklabels=MONTHS)
    axes[1].legend(fontsize=9)
    fig.tight_layout(); fig.savefig(out/'load_costs_and_solar_value.png', dpi=170); plt.close(fig)


def report(a, m, config, out, n):
    rows = {row['metric']: row for row in a.iter_rows(named=True)}
    def mean(key):
        return rows[key]['mean']
    def months(key):
        return m.filter(pl.col('metric') == key).sort('month')['mean'].to_numpy()
    lines = ['# Household bootstrap: paired saldering comparison', '',
             f'{n} alternative annual realizations; 2027 calendar, Europe/Amsterdam. Not a 100-year forecast.', '',
             '| Annual metric | Mean | 5th percentile | 95th percentile |', '|---|---:|---:|---:|']
    for key in ['with_saldering_total_eur', 'without_saldering_total_eur', 'without_minus_with_saldering_eur',
                'total_consumption_kwh', 'solar_production_kwh', 'grid_import_kwh', 'grid_export_kwh',
                'self_consumption_kwh', 'heat_pump_consumption_kwh', 'boiler_consumption_kwh',
                'phev_consumption_kwh', 'general_consumption_kwh']:
        r = rows[key]
        lines.append(f'| {key} | {r["mean"]:.2f} | {r["p05"]:.2f} | {r["p95"]:.2f} |')
    lines += ['', '## Bill lines (mean EUR/year)', '',
              '| Line | With saldering | Without saldering |', '|---|---:|---:|']
    for i, name in enumerate(['grid_import', 'saldering_credit', 'export_remuneration',
                             'Vaste leveringskosten', 'Systeembeheerkosten', 'Vermindering energiebelasting', 'Terugleverkosten']):
        lines.append(f'| {name} | {mean(f"with_saldering_line_{i}_{name}_eur"):.2f} | {mean(f"without_saldering_line_{i}_{name}_eur"):.2f} |')
    p = config['costs']['import_single_eur_kwh']
    r = config['costs']['export_eur_kwh']
    f = config['costs']['export_charges'][0]['eur_kwh']
    benefit = months('export_origin_saldering_benefit_eur')
    heating_season = [0, 1, 2, 9, 10, 11]
    lines += ['', '## Main explanations', '',
              f'- October–March accounts for {100*months("grid_import_kwh")[heating_season].sum()/mean("grid_import_kwh"):.1f}% of annual grid imports.',
              f'- April–September exports create {100*benefit[3:9].sum()/benefit.sum():.1f}% of the annual policy benefit.',
              f'- Direct solar avoids {p*100:.3f} ct/kWh in imports. Exports retain {(r-f)*100:.3f} ct/kWh without saldering, or {(p-f)*100:.3f} ct/kWh when fully netted.',
              f'- The policy difference is {(p-r)*100:.3f} ct per annually netted kWh; the same gross-export fee appears in both regimes.',
              f'- Moving an existing load to use one otherwise exported PV kWh is worth {(p-r+f)*100:.3f} ct without saldering, versus {f*100:.3f} ct with full netting, before any operational constraints. This is a marginal rate calculation, not a demonstrated feasible saving.', '',
              '## Seasonal results (ensemble means)', '',
              '| Month | Demand kWh | PV kWh | Import kWh | Export kWh | Bill with EUR | Bill without EUR | Benefit created by exports EUR |',
              '|---|---:|---:|---:|---:|---:|---:|---:|']
    keys = ['total_consumption_kwh', 'solar_production_kwh', 'grid_import_kwh', 'grid_export_kwh',
            'with_saldering_total_eur', 'without_saldering_total_eur', 'export_origin_saldering_benefit_eur']
    monthly_values = [months(key) for key in keys]
    for i, month in enumerate(MONTHS):
        lines.append('| '+month+' | '+' | '.join(f'{values[i]:.1f}' for values in monthly_values)+' |')
    lines += ['', '## Load contribution to gross imports', '',
              '| Load | Annual electricity kWh | Allocated gross import cost EUR |', '|---|---:|---:|']
    for load, label in zip(LOADS, LABELS):
        lines.append(f'| {label} | {mean(f"{load}_consumption_kwh"):.1f} | {months(f"{load}_gross_import_eur").sum():.1f} |')
    lines += ['', '## Inputs and interpretation', '',
        'KNMI De Bilt 2015–2025 observations resampled jointly in seven-day UTC blocks, within a ±21-day seasonal window, with at most 3°C jumps at block joins. Seeds 42–141. Stationary historical climate; no warming trend.',
        'Solar: saved 2023–2025 mean monthly yields and observed daily limits; 3.85 kW power cap. Radiation redistributes daily solar within fixed monthly targets. Annual sunshine uncertainty is not included.',
        'Heating: saved household setpoint 20.5°C, heated areas 55+10+8 m², base thermal/COP assumptions. Boiler: saved 200 L, 60°C, COP 3, draws at 08:30 and 19:00. These are assumption-based models, not calibrated appliance measurements.',
        'Car: saved 2521.2 kWh/year meter-side extrapolation, 3.5 kW from 17:45. General: saved 2500 kWh/year config with exploratory externally fitted shape; no load shifting. No separate car ERE credit is applied to this electricity bill.',
        'Both regimes use identical energy flows and supplied GreenChoice rates: import €0.24557/kWh, export receipt €0.15/kWh, gross-export fee €0.13613/kWh. All supplied fixed charges and tax credit are included. Contract tax treatment remains an assumption; future tariffs are not forecast.',
        'Bill = import × import rate − netted energy × import rate − remunerated export × export rate + gross-export fees + fixed charges. Netting is annual, min(import, export).',
        'Load import costs share each hour’s grid import in proportion to load consumption. These are explanatory allocations, not marginal savings from removing a load. Solar value is avoided grid cost plus net export value at unchanged loads and tariffs; it excludes installation, maintenance and degradation.',
        'Monthly settled costs allocate annual netting credits across import months. Export-origin benefit instead attributes the annual policy difference to the months producing exported energy. The two views answer different timing questions; neither is a monthly invoice.',
        'Spread covers sampled weather and timing only. Solar/general/car budgets, equipment, tariff uncertainty and structural model error are excluded. Hourly netting can understate actual meter import/export. No battery, gas top-up, capital cost or investment return is modeled.', '',
        '## Figures', '']
    for name in ['seasonal_energy', 'annual_bill', 'seasonal_cost_and_saldering', 'load_costs_and_solar_value']:
        lines.append(f'![{name}]({name}.png)\n')
    (out/'report.md').write_text('\n'.join(lines)+'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('data/overall/household.analysis.toml'))
    parser.add_argument('--weather-config', type=Path, default=Path('data/overall/weather.analysis.toml'))
    parser.add_argument('--history', type=Path, default=Path('data/weather_history_2015_2025'))
    parser.add_argument('--output', type=Path, default=Path('outputs/household_bootstrap_100'))
    run(parser.parse_args())
