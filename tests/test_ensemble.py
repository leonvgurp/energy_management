"""Reproducible ensembles, paired choices and billing before averaging."""
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from energy_management.config import load_config
from energy_management.ensemble import RunningMean, load_workflow, run, statistics
from energy_management.household import ENERGY_COLUMNS, simulate
from energy_management.costs import annual_cost, monthly_costs
from test_costs import contract, flows


def workflow(tmp_path, runs=2, alternatives=True):
    path = tmp_path/'workflow.toml'
    household = Path('configs/household.example.toml').resolve()
    # Real changed choice, including a shift which must preserve annual budgets.
    shifted = tmp_path/'shifted.toml'
    shifted.write_text(household.read_text().replace('fraction_moved = 0.0', 'fraction_moved = 0.7'))
    text = f'[ensemble]\nscenario_config = "{household}"\nruns = {runs}\nseed = 42\noutput = "result"\nplots = false\n'
    if alternatives:
        text += '[alternatives]\nshifted = "shifted.toml"\n'
    path.write_text(text)
    return path


def test_defaults_validation_and_input_protection(tmp_path):
    path = workflow(tmp_path)
    path.write_text(path.read_text().replace('runs = 2\n', ''))
    config, paths = load_workflow(path)
    assert config['runs'] == 100
    assert paths['shifted'] == tmp_path/'shifted.toml'
    with pytest.raises(ValueError, match='must not contain'):
        run(path, output=tmp_path)
    for value in ['0', '-1', 'true', '2.5']:
        path = workflow(tmp_path)
        path.write_text(path.read_text().replace('runs = 2', f'runs = {value}'))
        with pytest.raises(ValueError, match='runs'):
            load_workflow(path)
    path = workflow(tmp_path)
    path.write_text(path.read_text().replace('shifted =', 'reference ='))
    with pytest.raises(ValueError, match='names'):
        load_workflow(path)


def test_online_moments_and_single_sample():
    acc = RunningMean(['energy'])
    acc.add(pl.DataFrame({'key': ['a', 'b'], 'energy': [1., 3.]}))
    assert acc.std_frame()['energy'].null_count() == 2
    acc.add(pl.DataFrame({'key': ['a', 'b'], 'energy': [3., 7.]}))
    np.testing.assert_allclose(acc.frame()['energy'], [2, 5])
    np.testing.assert_allclose(acc.std_frame()['energy'], np.std([[1, 3], [3, 7]], axis=0, ddof=1))
    with pytest.raises(ValueError, match='align'):
        acc.add(pl.DataFrame({'key': ['b', 'a'], 'energy': [1., 3.]}))
    s = statistics(pl.DataFrame(dict(metric=['cost'], value=[10.])), ['metric'])
    assert s['std'][0] is None and s['standard_error'][0] is None


def test_real_ensemble_reproducibility_pairing_and_nonlinear_flows(tmp_path):
    path = workflow(tmp_path)
    result = run(path)
    out = tmp_path/'result'
    assert result['seeds'] == [42, 43]
    annual = pl.read_csv(out/'data/overall/annual_runs.txt', separator='\t')
    summary = pl.read_csv(out/'data/overall/annual_summary.txt', separator='\t')
    monthly = pl.read_csv(out/'data/overall/monthly_runs.txt', separator='\t')
    hourly = pl.read_csv(out/'data/overall/hourly_energy.txt', separator='\t')
    assert hourly.height == 8760
    assert hourly['interval_start_utc'].n_unique() == 8760
    assert ((hourly['grid_import_kwh'] > 0) & (hourly['grid_export_kwh'] > 0)).any()
    config = load_config('configs/household.example.toml')
    first = simulate(config)[0]
    config['scenario']['seed'] = 43
    second = simulate(config)[0]
    for c in ENERGY_COLUMNS:
        np.testing.assert_allclose(hourly[c], (first[c]+second[c])/2, atol=1e-12)
    for seed in [42, 43]:
        for variant in ['scenario', 'baseline']:
            for regime in ['with_saldering', 'without_saldering']:
                metric = f'{regime}_total_eur'
                actual = annual.filter((pl.col('seed') == seed) & (pl.col('variant') == variant) & (pl.col('metric') == metric))['value'][0]
                allocated = monthly.filter((pl.col('seed') == seed) & (pl.col('variant') == variant) & (pl.col('metric') == metric))['value'].sum()
                assert actual == pytest.approx(allocated)
    assert summary.filter(pl.col('variant') == 'baseline_minus_scenario')['mean'].abs().max() == 0
    choices = pl.read_csv(out/'data/overall/choice_differences_runs.txt', separator='\t')
    # Shifting changes costs without creating or destroying energy.
    assert choices.filter(pl.col('metric') == 'total_consumption_kwh')['value'].abs().max() < 1e-9
    assert choices.filter(pl.col('metric') == 'without_saldering_total_eur')['value'].abs().max() > 0
    saved = hourly.clone()
    rerun = run(path, output=tmp_path/'repeat')
    assert rerun['summary'] == result['summary']
    assert pl.read_csv(tmp_path/'repeat/data/overall/hourly_energy.txt', separator='\t').equals(saved)
    daily = pl.read_csv(out/'data/overall/daily_energy.txt', separator='\t')
    assert sorted(set(daily['hours'])) == [23, 24, 25]
    # Annual expected bills are the average of the realized annual bills.
    expected = (annual_cost(first, config['costs'], saldering=False)['total_eur']+
                annual_cost(second, config['costs'], saldering=False)['total_eur'])/2
    assert result['summary']['reference']['without_saldering_total_eur'] == pytest.approx(expected)


@pytest.mark.parametrize('enabled', [True, False])
@pytest.mark.parametrize('allocation', ['proportional', 'normal_first', 'offpeak_first'])
@pytest.mark.parametrize('imports,exports', [(1000, 600), (600, 1000), (0, 0), (0, 1000)])
def test_monthly_settlement_reconciles_lines(enabled, allocation, imports, exports):
    config = contract()
    config.update(meter='dual', netting_allocation=allocation)
    config['export_charges'][0].update(basis='excess_export', eur_day=.2, eur_year=20,
        tiers=[dict(up_to_kwh=2000, eur_year=50)])
    config['fixed'][0]['eur_year'] = 100
    frame = flows(imports, exports, year=2028)
    # Spread import/export over winter and summer and normal/offpeak registers.
    imp, exp = np.zeros(frame.height), np.zeros(frame.height)
    imp[[0, 4000, 5000]] = np.array([.2, .3, .5])*imports
    exp[[2, 4500]] = np.array([.4, .6])*exports
    frame = frame.with_columns(pl.Series('grid_import_kwh', imp), pl.Series('grid_export_kwh', exp))
    annual = annual_cost(frame, config, saldering=enabled)
    monthly = monthly_costs(frame, config, saldering=enabled)
    assert len(monthly) == 12
    assert sum(m['total_eur'] for m in monthly) == pytest.approx(annual['total_eur'])
    for i, line in enumerate(annual['lines']):
        assert sum(m['lines'][i]['eur'] for m in monthly) == pytest.approx(line['eur'])


def test_export_fee_threshold_is_applied_before_averaging(tmp_path, monkeypatch):
    import energy_management.ensemble as module
    path = workflow(tmp_path, alternatives=False)
    detailed = tmp_path/'tiered.toml'
    source = Path('configs/household.example.toml').resolve()
    detailed.write_text(source.read_text().replace('tiers = []',
        'tiers = [{up_to_kwh=400, eur_year=0}, {up_to_kwh=2000, eur_year=100}]'))
    path.write_text(path.read_text().replace(str(source), str(detailed)))
    config = load_config(detailed)
    template, _, diagnostics, _ = simulate(config)
    realized = []
    for export in (100., 600.):
        imports, exports = np.zeros(template.height), np.zeros(template.height)
        imports[0], exports[1] = 1000., export
        values = {c: np.zeros(template.height) for c in ENERGY_COLUMNS}
        for c in ('solar_production_kwh', 'total_production_kwh', 'grid_export_kwh'):
            values[c] = exports
        for c in ('general_consumption_kwh', 'total_consumption_kwh', 'grid_import_kwh'):
            values[c] = imports
        values['net_balance_kwh'] = exports-imports
        realized.append(template.with_columns(*[pl.Series(c, a) for c, a in values.items()],
            pl.Series('overproduction', exports > 0), pl.Series('high_consumption', imports > 0)))

    def fake_simulation(config, *args):
        frame = realized[config['scenario']['seed']-42]
        return frame, {}, diagnostics, frame

    monkeypatch.setattr(module, 'simulate', fake_simulation)
    result = run(path)
    expected = np.mean([annual_cost(f, config['costs'])['total_eur'] for f in realized])
    assert result['summary']['reference']['with_saldering_total_eur'] == pytest.approx(expected)
    mean = pl.read_csv(tmp_path/'result/data/overall/hourly_energy.txt', separator='\t')
    # The average year has 350 kWh export (zero tier fee), but half the actual
    # years cross 400 kWh and incur EUR 100: expected fee is EUR 50.
    assert expected-annual_cost(mean, config['costs'])['total_eur'] == pytest.approx(50.)
