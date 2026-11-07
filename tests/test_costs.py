"""Explainable annual bills and calendar/measurement boundaries."""
from copy import deepcopy

import polars as pl
import pytest

from energy_management.calendar import calendar_hours
from energy_management.config import load_config
from energy_management.costs import annual_cost, comparison, validate


def contract():
    return deepcopy(load_config('configs/household.example.toml')['costs'])


def flows(import_kwh=1000, export_kwh=600, year=2026):
    utc, _ = calendar_hours(year, 'Europe/Amsterdam')
    n = len(utc)
    # Only two nonzero readings: exact, easily audited energy totals.
    return pl.DataFrame(dict(interval_start_utc=[t.isoformat() for t in utc],
                             grid_import_kwh=[float(import_kwh)]+[0.]*(n-1),
                             grid_export_kwh=[0., float(export_kwh)]+[0.]*(n-2)))


def test_greenchoice_policy_equations():
    c = contract()
    result = comparison(flows(), c)
    fixed = 365*(.30635+1.30620-1.72317)
    assert result['with_saldering']['total_eur'] == pytest.approx(400*.24557+600*.13613+fixed)
    assert result['without_saldering']['total_eur'] == pytest.approx(1000*.24557-600*.15+600*.13613+fixed)
    assert result['without_minus_with_eur'] == pytest.approx(600*(.24557-.15))
    c['saldering'] = False
    assert comparison(flows(), c)['selected']['saldering'] is False


@pytest.mark.parametrize('imports,exports', [(0,0),(0,1000),(600,1000),(1000,1000)])
def test_export_surplus_and_no_double_credit(imports, exports):
    c = contract()
    bill = annual_cost(flows(imports, exports), c)
    net = min(imports, exports)
    assert bill['netted_kwh'] == net
    assert bill['remunerated_export_kwh'] == exports-net
    assert bill['total_eur'] == pytest.approx((imports-net)*.24557-(exports-net)*.15+exports*.13613+365*(.30635+1.30620-1.72317))


def test_daily_leap_year_dst_and_bad_measurements():
    c = contract()
    f = flows(year=2028)
    assert annual_cost(f, c)['days'] == 366
    for bad in (f.slice(1), f[[0,0]+list(range(2,f.height))], f.reverse(),
                f.with_columns(pl.lit(None).alias('grid_import_kwh')),
                f.with_columns(pl.lit(float('nan')).alias('grid_import_kwh')),
                f.with_columns(pl.lit(-1).alias('grid_export_kwh'))):
        with pytest.raises(ValueError):
            annual_cost(bad, c)


def test_step_fee_boundaries_and_basis():
    c = contract()
    line = c['export_charges'][0]
    line.update(eur_kwh=0, eur_day=1, eur_year=10,
                tiers=[dict(up_to_kwh=500, eur_year=0), dict(up_to_kwh=1000, eur_year=70)])
    def fee(exports):
        return annual_cost(flows(1000,exports), c)['lines'][-1]['eur']
    assert fee(0) == 0
    assert fee(500) == 375
    assert fee(501) == 445
    assert fee(1000) == 445
    with pytest.raises(ValueError, match='final tier'):
        fee(1001)
    line['basis'] = 'excess_export'
    assert fee(1000) == 0


def test_dual_register_allocation_and_local_schedule():
    c = contract()
    c.update(meter='dual', import_normal_eur_kwh=.2, import_offpeak_eur_kwh=.3)
    c['fixed'] = []
    c['export_charges'] = []
    f = flows(0, 0)
    utc, local = calendar_hours(2026, 'Europe/Amsterdam')
    normal = next(i for i,t in enumerate(local) if t.weekday() == 0 and t.hour == 12)
    offpeak = next(i for i,t in enumerate(local) if t.weekday() == 0 and t.hour == 1)
    imports, exports = [0.]*len(utc), [0.]*len(utc)
    imports[normal], imports[offpeak], exports[2] = 100, 100, 100
    f = f.with_columns(pl.Series('grid_import_kwh',imports), pl.Series('grid_export_kwh',exports))
    for policy, cost in [('proportional',25),('normal_first',30),('offpeak_first',20)]:
        c['netting_allocation'] = policy
        assert annual_cost(f,c)['total_eur'] == pytest.approx(cost)
    c['offpeak_dates'] = [local[normal].strftime('%m-%d')]
    assert annual_cost(f,c)['total_eur'] == pytest.approx(30)


@pytest.mark.parametrize('key,value', [('saldering',1),('meter','dynamic'),('export_eur_kwh',float('inf')),('offpeak_start_hour',24),('typo',1)])
def test_invalid_tariff_rejected(key,value):
    c = contract()
    c[key] = value
    with pytest.raises(ValueError):
        validate(c)
