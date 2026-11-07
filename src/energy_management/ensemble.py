"""Repeated synthetic years, paired choices and expected annual electricity bills."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import tomllib

import numpy as np
import polars as pl

from .config import REFERENCE_YEAR, load_config
from .costs import comparison, monthly_costs
from .energy_tables import write_energy_tables
from .household import ENERGY_COLUMNS, MODALITIES, daily_totals, simulate


class RunningMean:
    """Welford moments without storing all hourly realizations in memory."""
    def __init__(self, columns):
        self.columns = list(columns)
        self.n = 0

    def add(self, frame):
        values = frame.select(self.columns).to_numpy().astype(float)
        if not np.isfinite(values).all():
            raise ValueError('Ensemble values must be finite and nonmissing')
        if not self.n:
            self.keys = frame.drop(self.columns)
            self.mean = np.zeros_like(values)
            self.m2 = np.zeros_like(values)
        elif not self.keys.equals(frame.drop(self.columns)):
            raise ValueError('Ensemble timestamps/keys must align exactly')
        self.n += 1
        delta = values-self.mean
        self.mean += delta/self.n
        self.m2 += delta*(values-self.mean)

    def frame(self):
        return self.keys.hstack(pl.DataFrame(self.mean, schema=self.columns))

    def std_frame(self):
        values = np.sqrt(np.maximum(0, self.m2/(self.n-1))) if self.n > 1 else np.full_like(self.mean, np.nan)
        return self.keys.hstack(pl.DataFrame(values, schema=self.columns).fill_nan(None))


def load_workflow(path):
    path = Path(path).resolve()
    with path.open('rb') as stream:
        raw = tomllib.load(stream)
    if set(raw)-{'ensemble', 'alternatives'} or 'ensemble' not in raw:
        raise ValueError('Workflow requires [ensemble] and optional [alternatives]')
    c = {**dict(runs=100, seed=42, calendar_year=REFERENCE_YEAR,
               output='../outputs/ensemble', plots=True), **raw['ensemble']}
    if set(c) != {'scenario_config', 'runs', 'seed', 'calendar_year', 'output', 'plots'}:
        raise ValueError('Unknown or missing ensemble setting; require scenario_config')
    for key, minimum in [('runs', 1), ('seed', 0), ('calendar_year', 1)]:
        if type(c[key]) is not int or c[key] < minimum:
            raise ValueError(f'ensemble.{key} must be an integer >= {minimum}')
    if not 1 <= c['calendar_year'] <= 9998 or type(c['plots']) is not bool:
        raise ValueError('Invalid calendar_year or plots setting')
    for key in ('scenario_config', 'output'):
        if not isinstance(c[key], str) or not c[key].strip():
            raise ValueError(f'ensemble.{key} must be a nonempty path')
    scenarios = {'reference': (path.parent/c['scenario_config']).resolve()}
    alternatives = raw.get('alternatives', {})
    for name, value in alternatives.items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', name) or name == 'reference':
            raise ValueError('Alternative names must be safe directory names other than reference')
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Each alternative must point to a household TOML config')
        scenarios[name] = (path.parent/value).resolve()
    c['output'] = str((path.parent/c['output']).resolve())
    return c, scenarios


def statistics(frame, keys):
    """Sample spread and Monte Carlo error; quantiles describe years, not means."""
    return frame.group_by(keys, maintain_order=True).agg(
        pl.len().alias('samples'), pl.col('value').mean().alias('mean'),
        pl.col('value').std(ddof=1).alias('std'),
        (pl.col('value').std(ddof=1)/pl.len().sqrt()).alias('standard_error'),
        *[pl.col('value').quantile(q, interpolation='linear').alias(name)
          for q, name in [(.05, 'p05'), (.5, 'p50'), (.95, 'p95')]])


def energy_metrics(frame):
    values = {key: float(frame[key].sum()) for key in ENERGY_COLUMNS}
    values.update(peak_consumption_kwh=float(frame['total_consumption_kwh'].max()),
                  overproduction_hours=float(frame['overproduction'].sum()))
    production, consumption = values['total_production_kwh'], values['total_consumption_kwh']
    values['self_consumption_fraction'] = values['self_consumption_kwh']/production if production else 0.
    values['self_sufficiency_fraction'] = values['self_consumption_kwh']/consumption if consumption else 0.
    return values


def unit(metric):
    return 'EUR' if metric.endswith('_eur') else ('fraction' if metric.endswith('_fraction') else
           ('hours' if metric.endswith('_hours') else 'kWh'))


def bill_metrics(bills):
    values = {}
    for regime in ('with_saldering', 'without_saldering'):
        bill = bills[regime]
        values[f'{regime}_total_eur'] = bill['total_eur']
        values[f'{regime}_netted_kwh'] = bill['netted_kwh']
        values[f'{regime}_remunerated_export_kwh'] = bill['remunerated_export_kwh']
        for i, line in enumerate(bill['lines']):
            values[f'{regime}_line_{i}_{line["name"]}_eur'] = line['eur']
    values['without_minus_with_saldering_eur'] = bills['without_minus_with_eur']
    return values


def record(rows, values, **keys):
    rows.extend(dict(**keys, metric=metric, unit=unit(metric), value=value)
                for metric, value in values.items())


def paired_difference(frame, positive, negative, label):
    keys = [c for c in frame.columns if c not in ('variant', 'value')]
    return (frame.filter(pl.col('variant') == positive)
            .join(frame.filter(pl.col('variant') == negative), on=keys, suffix='_other', validate='1:1')
            .with_columns(pl.lit(label).alias('variant'), (pl.col('value')-pl.col('value_other')).alias('value'))
            .select(frame.columns))


def _write(frame, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_csv(path, separator='\t')


def _cost_plots(annual, monthly, dest):
    import matplotlib.pyplot as plt
    dest.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(9, 5))
    for offset, variant in [(-.18, 'baseline'), (.18, 'scenario')]:
        rows = annual.filter((pl.col('variant') == variant) &
            pl.col('metric').is_in(['with_saldering_total_eur', 'without_saldering_total_eur']))
        # Use explicit order independent of lexical spelling.
        rows = [rows.filter(pl.col('metric') == f'{r}_total_eur').row(0, named=True)
                for r in ('with_saldering', 'without_saldering')]
        means = np.array([r['mean'] for r in rows])
        ax.bar(np.arange(2)+offset, means, width=.36, label=variant)
        ax.vlines(np.arange(2)+offset, [r['p05'] for r in rows], [r['p95'] for r in rows], color='black')
    ax.set(xticks=[0, 1], xticklabels=['With saldering', 'Without saldering'],
           ylabel='Expected annual electricity bill (EUR/year)',
           title='Mean annual cost · lines show 5–95% of simulated years')
    ax.axhline(0, color='grey', linewidth=.6)
    ax.legend()
    fig.tight_layout()
    fig.savefig(dest/'annual_costs.png', dpi=150)
    plt.close(fig)
    fig, ax = plt.subplots(figsize=(10, 5))
    for regime in ('with_saldering', 'without_saldering'):
        for variant, style in [('scenario', '-'), ('baseline', '--')]:
            rows = monthly.filter((pl.col('variant') == variant) &
                                  (pl.col('metric') == f'{regime}_total_eur')).sort('month')
            ax.plot(range(1, 13), rows['mean'], style, label=f'{variant}: {regime.replace("_", " ")}')
    ax.set(xlabel='Local calendar month', ylabel='Allocated annual bill (EUR/month)',
           title='Mean monthly contribution to annual settlement', xticks=range(1, 13))
    ax.legend(fontsize=8)
    ax.grid(alpha=.2)
    fig.tight_layout()
    fig.savefig(dest/'monthly_costs.png', dpi=150)
    plt.close(fig)


def run(config_path, output=None, make_plots=None, progress=None):
    """Run each config with the same seed sequence; bill BEFORE aggregating."""
    config_path = Path(config_path).resolve()
    workflow, paths = load_workflow(config_path)
    output = Path(output or workflow['output']).resolve()
    configs = {name: load_config(path) for name, path in paths.items()}
    zones = {c['scenario']['timezone'] for c in configs.values()}
    if len(zones) != 1:
        raise ValueError('Paired scenarios require the same timezone')
    if any('costs' not in c for c in configs.values()):
        raise ValueError('Every ensemble scenario requires a [costs] section')
    inputs = {config_path, *paths.values()}
    for name, c in configs.items():
        if c['weather']['mode'] == 'file':
            inputs.add((paths[name].parent/c['weather']['file']).resolve())
    if any(p == output or output in p.parents for p in inputs):
        raise ValueError('Output directory must not contain configs or source weather')
    # Unique, stable seed prefix even when the run count grows.
    seeds = [workflow['seed']+i for i in range(workflow['runs'])]
    make_plots = workflow['plots'] if make_plots is None else make_plots
    workflow = dict(workflow, output=str(output), plots=make_plots)
    year = workflow['calendar_year']
    all_annual = []
    summaries = {}
    for name, config in configs.items():
        dest = output if name == 'reference' else output/'scenarios'/name
        annual_rows, monthly_rows = [], []
        moments = {variant: RunningMean([*ENERGY_COLUMNS, 'overproduction', 'high_consumption'])
                   for variant in ('scenario', 'baseline')}
        climate_mean = None
        for index, seed in enumerate(seeds):
            realization = deepcopy(config)
            realization['scenario']['seed'] = seed
            hourly, _, diagnostics, baseline = simulate(realization, year, paths[name].parent)
            climate = diagnostics['weather']
            if climate_mean is None:
                climate_mean = RunningMean([c for c, dtype in climate.schema.items() if dtype.is_numeric()])
                events = diagnostics['solar'].select('date', 'sunrise_hour', 'solar_noon_hour', 'sunset_hour')
            climate_mean.add(climate)
            for variant, frame in [('scenario', hourly), ('baseline', baseline)]:
                moments[variant].add(frame)
                bills = comparison(frame, config['costs'], config['scenario']['timezone'])
                record(annual_rows, {**energy_metrics(frame), **bill_metrics(bills)},
                       run=index, seed=seed, variant=variant)
                monthly = daily_totals(frame).with_columns(pl.col('date').str.slice(0, 7).alias('month')).group_by('month').agg(
                    *[pl.col(c).sum() for c in ENERGY_COLUMNS]).sort('month')
                for row in monthly.iter_rows(named=True):
                    month = row.pop('month')
                    record(monthly_rows, row, run=index, seed=seed, variant=variant, month=month)
                for enabled, regime in [(True, 'with_saldering'), (False, 'without_saldering')]:
                    for row in monthly_costs(frame, config['costs'], config['scenario']['timezone'], enabled):
                        values = {f'{regime}_total_eur': row['total_eur']}
                        values.update({f'{regime}_line_{i}_{line["name"]}_eur': line['eur']
                                       for i, line in enumerate(row['lines'])})
                        record(monthly_rows, values, run=index, seed=seed, variant=variant, month=row['month'])
            if progress:
                progress(f'{name}: {index+1}/{len(seeds)} years complete (seed {seed})')
        annual, monthly = pl.DataFrame(annual_rows), pl.DataFrame(monthly_rows)
        annual = pl.concat([annual, paired_difference(annual, 'baseline', 'scenario', 'baseline_minus_scenario')])
        monthly = pl.concat([monthly, paired_difference(monthly, 'baseline', 'scenario', 'baseline_minus_scenario')])
        annual_stats = statistics(annual, ['variant', 'metric', 'unit'])
        monthly_stats = statistics(monthly, ['variant', 'month', 'metric', 'unit'])
        root = dest/'data/overall'
        for filename, frame in [('annual_runs', annual), ('annual_summary', annual_stats),
                                ('monthly_runs', monthly), ('monthly_summary', monthly_stats)]:
            _write(frame, root/f'{filename}.txt')
        mean = moments['scenario'].frame()
        # Averaged flags are probabilities, never booleans on an averaged profile.
        for variant, accumulator in moments.items():
            frame = accumulator.frame().rename({'overproduction': 'overproduction_probability',
                                                'high_consumption': 'high_consumption_probability'})
            prefix = '' if variant == 'scenario' else 'baseline_'
            _write(frame, root/f'{prefix}hourly_energy.txt')
            _write(daily_totals(frame), root/f'{prefix}daily_energy.txt')
            _write(accumulator.std_frame().select('interval_start_utc', 'interval_start_local', *ENERGY_COLUMNS),
                   root/f'{prefix}hourly_energy_std.txt')
        for modality in MODALITIES:
            column = f'{modality}_{"production" if modality == "solar" else "consumption"}_kwh'
            write_energy_tables(mean.select('interval_start_utc', 'interval_start_local', pl.col(column).alias('energy_kwh')),
                                dest/f'data/{modality}', 'energy_kwh', config['scenario']['timezone'])
        _write(climate_mean.frame(), dest/'data/weather/diagnostics.txt')
        _write(events, dest/'data/solar/diagnostics.txt')
        summary = {row['metric']: row['mean'] for row in annual_stats.filter(pl.col('variant') == 'scenario').iter_rows(named=True)}
        summaries[name] = {key: summary[key] for key in ('total_consumption_kwh', 'grid_import_kwh',
            'grid_export_kwh', 'with_saldering_total_eur', 'without_saldering_total_eur', 'without_minus_with_saldering_eur')}
        dest.mkdir(parents=True, exist_ok=True)
        (dest/'scenario.toml').write_bytes(paths[name].read_bytes())
        metadata = dict(schema_version=1, kind='synthetic_year_ensemble', config=config,
                        reference_calendar_year=year, seeds=seeds, summary=summary,
                        annual_statistics=annual_stats.to_dicts())
        if make_plots:
            from .plots import plot_outputs
            metadata['selected_days'] = plot_outputs(mean, dest/'plots', config['plots'],
                weather=climate_mean.frame(), solar_events=events, ensemble_runs=len(seeds))
            _cost_plots(annual_stats, monthly_stats, dest/'plots/overall')
        (dest/'run.json').write_text(json.dumps(metadata, indent=2, allow_nan=False)+'\n')
        all_annual.append(annual.with_columns(pl.lit(name).alias('choice')))
    combined = pl.concat(all_annual)
    _write(statistics(combined, ['choice', 'variant', 'metric', 'unit']), output/'data/overall/choices_summary.txt')
    if len(paths) > 1:
        paired = []
        keys = ['run', 'seed', 'variant', 'metric', 'unit']
        reference = combined.filter(pl.col('choice') == 'reference')
        for name in paths:
            if name == 'reference':
                continue
            joined = reference.join(combined.filter(pl.col('choice') == name), on=keys, suffix='_alternative', validate='1:1')
            paired.append(joined.select(*keys, pl.lit(name).alias('alternative'),
                          (pl.col('value')-pl.col('value_alternative')).alias('value')))
        differences = pl.concat(paired)
        _write(differences, output/'data/overall/choice_differences_runs.txt')
        _write(statistics(differences, ['alternative', 'variant', 'metric', 'unit']),
               output/'data/overall/choice_differences_summary.txt')
    (output/'workflow.toml').write_bytes(config_path.read_bytes())
    provenance = dict(workflow=workflow, seeds=seeds, scenarios={k: str(v) for k, v in paths.items()},
        summary=summaries,
        source_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(inputs)},
        implementation_sha256={str(p.relative_to(Path(__file__).parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted(Path(__file__).parent.rglob('*.py'))},
        assumptions=[
            'Monte Carlo synthetic-year ensemble, not resampling measured historical years or a climate forecast.',
            'All detailed assumptions stay fixed; workflow seeds replace scenario.seed. Alternatives use identical seed labels.',
            'Grid flows, annual bills, tier fees and paired differences are calculated per realization before averaging.',
            'Mean hourly import and export can both be positive because they average different years.',
            'Spread and 5/95 percentiles describe simulated years conditional on configured assumptions; standard error describes the estimated mean.',
            'Fixed monthly/annual budgets do not acquire interannual uncertainty from new seeds. File weather is identical across seeds.',
            'Baseline disables only general-use shifting. Choice differences are reference minus alternative; positive EUR means savings.',
            'Monthly costs allocate annual settlement; netting credits follow register imports, receipts follow exports, annual fees follow calendar days.',
            'Both policy regimes hold configured tariffs constant; these estimates are not future contract quotes.',
            'Reference calendar controls weekday, leap-year and DST labels, not forecast year.'])
    (output/'ensemble.json').write_text(json.dumps(provenance, indent=2, allow_nan=False)+'\n')
    return provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('configs/ensemble.example.toml'))
    parser.add_argument('--output', type=Path, help='Override workflow output directory')
    parser.add_argument('--no-plots', action='store_true')
    args = parser.parse_args()
    result = run(args.config, args.output, False if args.no_plots else None,
                 progress=lambda message: print(message, flush=True))
    print(json.dumps(result['summary'], indent=2))


if __name__ == '__main__':
    main()
