"""Integration and accounting checks on the shared annual pipeline."""
from copy import deepcopy
from datetime import datetime
import json

import matplotlib.pyplot as plt
import numpy as np
import polars as pl
import pytest

from energy_management.config import load_config, validate_config
from energy_management.household import MODALITIES, combine, daily_totals, run, simulate
from energy_management.plots import calendar_heatmap, day_profile, select_example_days


@pytest.fixture(scope='module')
def config():
    return load_config('configs/household.example.toml')


@pytest.fixture(scope='module')
def annual(config):
    return simulate(config)


def test_annual_accounting_and_model_budgets(config, annual):
    frame, components, diagnostics, baseline = annual
    assert frame.height == 8760
    assert frame['interval_start_utc'].n_unique() == 8760
    assert frame['interval_start_local'][0].startswith('2001-01-01T00:00')
    assert frame['interval_start_local'][-1].startswith('2001-12-31T23:00')
    np.testing.assert_allclose(frame['total_consumption_kwh'], sum(frame[f'{m}_consumption_kwh'] for m in MODALITIES[1:]))
    np.testing.assert_allclose(frame['total_production_kwh']+frame['grid_import_kwh'], frame['total_consumption_kwh']+frame['grid_export_kwh'])
    np.testing.assert_allclose(frame['self_consumption_kwh']+frame['grid_export_kwh'], frame['solar_production_kwh'])
    assert not ((frame['grid_import_kwh'] > 0) & (frame['grid_export_kwh'] > 0)).any()
    assert frame['general_consumption_kwh'].sum() == pytest.approx(config['general']['annual_total_kwh'])
    assert frame['solar_production_kwh'].sum() == pytest.approx(sum(config['solar']['monthly_kwh']))
    assert frame['solar_production_kwh'].max() <= config['solar']['max_power_kw']
    assert frame['phev_consumption_kwh'].max() <= config['phev']['charging_power_kw']
    assert frame.equals(baseline)  # Zero movement is exactly the baseline.
    assert daily_totals(frame)['hours'].sort().to_list().count(23) == 1
    assert daily_totals(frame)['hours'].sort().to_list().count(25) == 1
    assert diagnostics['boiler'].height == 8760
    assert set(components) == set(MODALITIES)


def test_missing_duplicate_nonfinite_or_wrong_year_rejected(annual):
    _, components, _, _ = annual
    for bad in (components['boiler'].slice(1), components['boiler'][[0,0]+list(range(2,8760))],
                components['boiler'].with_columns(pl.lit(float('nan')).alias('energy_kwh'))):
        with pytest.raises(ValueError):
            combine(dict(components, boiler=bad), 2001, 'Europe/Amsterdam')
    with pytest.raises(ValueError, match='full reference calendar'):
        combine(components, 2002, 'Europe/Amsterdam')


def test_selected_days_are_seasonal_extrema(annual):
    frame = annual[0]
    daily = daily_totals(frame)
    chosen = select_example_days(frame)
    assert len(chosen) == 4
    for row in chosen:
        months = ['12','01','02'] if row['label'].startswith('winter') else ['06','07','08']
        values = daily.filter(pl.col('date').str.slice(5,2).is_in(months))['net_balance_kwh']
        assert row['net_balance_kwh'] == (values.max() if row['label'].endswith('best') else values.min())


def test_leap_year_and_weather_sensitivity(config, annual):
    changed = deepcopy(config)
    changed['weather']['sunshine_factor'] = .8
    changed['weather']['winter_temperature_offset_c'] = -3
    frame, _, diagnostics, _ = simulate(changed, 2004)
    assert frame.height == 8784
    assert daily_totals(frame).height == 366
    assert frame['solar_production_kwh'].sum() == pytest.approx(.8*sum(config['solar']['monthly_kwh']))
    assert frame['heat_pump_consumption_kwh'].sum() > annual[0]['heat_pump_consumption_kwh'].sum()
    assert diagnostics['weather']['radiation_kwh_m2'].sum() == pytest.approx(.8*sum(config['weather']['monthly_radiation_kwh_m2']))


def test_file_weather_preserved_and_shift_baseline(config, annual, tmp_path):
    path = tmp_path/'weather.txt'
    annual[2]['weather'].write_csv(path, separator='\t')
    before = path.read_bytes()
    changed = deepcopy(config)
    changed['weather'].update(mode='file', file='weather.txt')
    changed['general']['evening_shift']['fraction_moved'] = .7
    frame, _, diagnostics, baseline = simulate(changed, config_dir=tmp_path)
    assert path.read_bytes() == before
    assert diagnostics['weather'].equals(annual[2]['weather'])
    assert not frame.equals(baseline)
    assert frame['total_consumption_kwh'].sum() == pytest.approx(baseline['total_consumption_kwh'].sum())
    assert frame['solar_production_kwh'].equals(baseline['solar_production_kwh'])
    daily_frame, daily_base = daily_totals(frame), daily_totals(baseline)
    np.testing.assert_allclose(daily_frame['general_consumption_kwh'], daily_base['general_consumption_kwh'])


def test_shared_plots_handle_dst(annual):
    frame = annual[0]
    daily = daily_totals(frame)
    for day, hours in [('03-25',23), ('10-28',25)]:
        fig = day_profile(frame, day, weather=annual[2]['weather'], solar_events=annual[2]['solar'])
        assert len(fig.axes) == 4  # Three panels plus the unitless sun axis.
        assert len(fig.axes[1].lines[0].get_xdata()) == hours
        weather_day = annual[2]['weather'].filter(pl.col('interval_start_local').str.slice(5, 5) == day)
        np.testing.assert_array_equal(fig.axes[0].lines[0].get_ydata(), weather_day['outdoor_c'].to_numpy())
        np.testing.assert_array_equal(fig.axes[3].lines[0].get_ydata(), [0, 0, 1, 0, 0])
        assert len(fig.axes[3].get_yticks()) == 0
        event = annual[2]['solar'].filter(pl.col('date').cast(pl.String).str.slice(5, 5) == day).row(0, named=True)
        sun_times = fig.axes[3].lines[0].get_xdata()[1:4]
        from zoneinfo import ZoneInfo
        for stamp, key in zip(sun_times, ('sunrise_hour', 'solar_noon_hour', 'sunset_hour')):
            local = stamp.astimezone(ZoneInfo('Europe/Amsterdam'))
            assert local.hour+local.minute/60+local.second/3600 == pytest.approx(event[key])
        plt.close(fig)
    fig = calendar_heatmap(daily, 'solar_production_kwh')
    assert fig.axes[0].collections[0].get_array().shape == (12,31)
    plt.close(fig)
    with pytest.raises(ValueError, match='No hourly data'):
        day_profile(frame, '2002-01-01', weather=annual[2]['weather'], solar_events=annual[2]['solar'])


def test_run_writes_common_and_modality_tables(tmp_path):
    output = tmp_path/'result'
    meta = run('configs/household.example.toml', output, make_plots=False)
    costs = json.loads((output/'data/overall/annual_costs.json').read_text())
    assert costs['scenario']['selected']['total_eur'] == meta['summary']['annual_cost_eur']
    assert costs['scenario'] == costs['baseline']
    assert meta['reference_calendar_year'] == 2001
    assert (output/'scenario.toml').is_file()
    assert json.loads((output/'run.json').read_text())['summary']['hours'] == 8760
    for name in MODALITIES:
        table = pl.read_csv(output/f'data/{name}/hourly_energy.txt', separator='\t')
        assert table.columns == ['interval_start_utc','interval_start_local','energy_kwh']
        assert table.height == 8760
    with pytest.raises(ValueError, match='must not contain'):
        run('configs/household.example.toml', 'configs', make_plots=False)


@pytest.mark.parametrize('section,key,value', [
    ('weather','sunshine_factor',-1), ('weather','winter_temperature_offset_c',float('nan')),
    ('weather','monthly_temperature_c',[1]*11), ('weather','winter_months',[0,1]),
    ('scenario','seed',-1), ('general','timezone','UTC'),
])
def test_invalid_shared_config(config, section, key, value):
    changed = deepcopy(config)
    changed[section][key] = value
    with pytest.raises(ValueError):
        validate_config(changed)
