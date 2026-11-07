import pytest
import polars as pl
from energy_management.car_schedule import generate, daily_budgets, monthly_targets


def config():
    return dict(timezone='Europe/Amsterdam', annual_meter_kwh=365*7.,
                charging_power_kw=3.5, charge_start='17:45')


@pytest.mark.parametrize('year,hours,days', [(2026,8760,365),(2024,8784,366)])
def test_annual_conservation_dst_and_daily_budget(year, hours, days):
    f = generate(config(),year)
    assert f.height == hours
    assert f['interval_start_utc'].n_unique() == hours
    assert f['energy_kwh'].sum() == pytest.approx(365*7.)
    daily = f.group_by(pl.col('interval_start_local').str.slice(0,10)).agg(pl.col('energy_kwh').sum(), pl.len().alias('hours'))
    assert daily.height == days
    assert daily['energy_kwh'].min() == pytest.approx(365*7./days)
    assert daily['energy_kwh'].max() == pytest.approx(365*7./days)
    assert daily['hours'].min() == 23
    assert daily['hours'].max() == 25


def test_partial_hour_allocation():
    f = generate(config(),2026)
    assert f['energy_kwh'][17:21].to_list() == pytest.approx([.875,3.5,2.625,0])


def test_shuffled_cycle_reproducible_discrete_and_monthly_close():
    c = config()
    c.update(daily_cycle_kwh=[0.,0.,3.,9.5,9.5,12.,12.], seed=42)
    a = daily_budgets(c,2026)
    assert a == daily_budgets(c,2026)
    assert set(a.values()) <= set(c['daily_cycle_kwh'])
    for month in range(1,13):
        values = [v for d,v in a.items() if d.month == month]
        assert 0 in values
        assert abs(sum(values)-monthly_targets(c,2026)[month-1]) < 3
    c['seed'] = 43
    assert a != daily_budgets(c,2026)
    f = generate(c,2026)
    assert f['energy_kwh'].sum() == pytest.approx(sum(daily_budgets(c,2026).values()))
    assert f['energy_kwh'].max() <= c['charging_power_kw']


def test_seasonal_targets_cap_and_zero_days():
    c = config()
    c.update(daily_cycle_kwh=[0.,0.,3.,9.5,9.5,12.,12.], seed=42,
             max_charge_kwh=13., seasonal_amplitude=.15)
    targets = monthly_targets(c,2026)
    assert sum(targets) == pytest.approx(c['annual_meter_kwh'])
    assert targets[0]/targets[6] == pytest.approx(1.15/.85)
    budgets = daily_budgets(c,2026)
    assert max(budgets.values()) == 13
    for month in range(1,13):
        values = [v for d,v in budgets.items() if d.month == month]
        assert 0 in values
        assert abs(sum(values)-targets[month-1]) < 3
    f = generate(c,2026)
    assert f['energy_kwh'].sum() == pytest.approx(sum(budgets.values()))


@pytest.mark.parametrize('year,feb_days', [(2026,28),(2024,29)])
def test_month_lengths_and_leap_year(year, feb_days):
    c = config()
    targets = monthly_targets(c,year)
    assert targets[0]/targets[1] == pytest.approx(31/feb_days)
    assert sum(targets) == pytest.approx(c['annual_meter_kwh'])
    c['seasonal_amplitude'] = .15
    targets = monthly_targets(c,year)
    assert (targets[0]/31)/(targets[6]/31) == pytest.approx(1.15/.85)
    assert sum(targets) == pytest.approx(c['annual_meter_kwh'])


def test_explicit_monthly_targets_are_not_reweighted():
    c = config()
    c['monthly_meter_kwh'] = [100.]*12
    assert monthly_targets(c,2024) == [100.]*12


@pytest.mark.parametrize('change', [dict(max_charge_kwh=0), dict(seasonal_amplitude=1),
    dict(daily_cycle_kwh=[14.]), dict(annual_meter_kwh=365*14.)])
def test_maximum_and_seasonal_validation(change):
    c = config(); c.update(change)
    with pytest.raises(ValueError):
        generate(c,2026)


@pytest.mark.parametrize('key,value', [('annual_meter_kwh',float('nan')),
    ('charging_power_kw',0), ('charge_start','02:30'), ('charge_start','23:45')])
def test_invalid_schedule(key,value):
    c = config(); c[key] = value
    with pytest.raises(ValueError):
        generate(c,2026)
