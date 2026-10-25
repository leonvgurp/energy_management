from copy import deepcopy
from datetime import date
import tomllib

import pytest

from energy_management.boiler import draw_energy, simulate, validate


def config():
    with open('configs/boiler.example.toml', 'rb') as f:
        return tomllib.load(f)


def test_energy_conversion_and_split():
    c = config()
    hp, element = draw_energy(c, 1)
    assert hp == pytest.approx(200 * 4.186 * 50 / 3600)
    assert element == 0
    c['setpoint_c'] = 65
    assert draw_energy(c, 1)[1] == pytest.approx(200 * 4.186 * 5 / 3600)


@pytest.mark.parametrize('start,end,intervals', [(date(2026,3,29), date(2026,3,30),46), (date(2026,10,25),date(2026,10,26),50)])
def test_dst_and_conservation(start, end, intervals):
    c = config()
    f = simulate(c, start, end)
    assert f.height == intervals
    assert f['interval_start_utc'].n_unique() == intervals
    delivered = f['heat_pump_heat_kwh'].sum()+f['resistance_heat_kwh'].sum()
    assert delivered + f['outstanding_heat_kwh'][-1] == pytest.approx(f['draw_heat_kwh'].sum())
    assert f['draw_heat_kwh'].sum() == pytest.approx(sum(draw_energy(c, 1))*1.5)
    assert f['standing_electric_kwh'].sum() == pytest.approx(intervals * .5 * .025)


def test_backlog_and_midnight_carry():
    c = config()
    c['heat_pump_thermal_kw'] = .1
    f = simulate(c, date(2026,1,1), date(2026,1,3))
    assert f['outstanding_heat_kwh'][-1] > 0
    assert f['heat_pump_heat_kwh'].sum()+f['outstanding_heat_kwh'][-1] == pytest.approx(f['draw_heat_kwh'].sum())


def test_no_demand_before_draw_and_completed_day():
    c = config()
    c['standing_electric_kw'] = 0
    f = simulate(c, date(2026,1,1), date(2026,1,2))
    assert f['consumption_kwh'][:17].sum() == 0
    assert f['outstanding_heat_kwh'][-1] == pytest.approx(0)
    assert f['consumption_kwh'].sum() == pytest.approx(sum(draw_energy(c, 1))*1.5/c['cop'])


@pytest.mark.parametrize('key,value', [('cop',0), ('cold_water_c',60), ('standing_electric_kw',-1), ('volume_l',float('nan'))])
def test_invalid_config(key,value):
    c = deepcopy(config())
    c[key] = value
    with pytest.raises(ValueError):
        validate(c)
