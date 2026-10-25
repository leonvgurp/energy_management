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


@pytest.mark.parametrize('day,hours', [(date(2026,1,1),24), (date(2026,3,29),23), (date(2026,10,25),25)])
def test_hourly_diagnostics_conserve_energy_and_end_state(day, hours):
    from datetime import timedelta
    from energy_management.boiler import hourly_diagnostics
    c = config()
    steps = simulate(c, day, day+timedelta(days=1))
    hourly = hourly_diagnostics(steps, c['timezone'])
    assert hourly.height == hours
    for name in steps.columns:
        if name.endswith('_kwh') and name != 'outstanding_heat_kwh':
            assert hourly[name].sum() == pytest.approx(steps[name].sum())
    assert hourly['outstanding_heat_kwh'].to_list() == steps['outstanding_heat_kwh'][1::2].to_list()
    # Morning draw remains in 08:00–09:00 with heating for half that hour.
    morning = next(r for r in hourly.iter_rows(named=True) if 'T08:00:00' in r['interval_start_local'])
    assert morning['heat_pump_heat_kwh'] == pytest.approx(c['heat_pump_thermal_kw']*.5)


def test_cli_exports_only_hourly_intervals(tmp_path, monkeypatch):
    import json
    from datetime import datetime
    import polars as pl
    from energy_management.boiler import main
    monkeypatch.setattr('sys.argv', ['boiler', '--config', 'configs/boiler.example.toml',
        '--start', '2026-10-25', '--end', '2026-10-26', '--output', str(tmp_path)])
    main()
    totals = []
    for name, energy in [('hourly_energy.txt','energy_kwh'), ('hourly_consumption.txt','consumption_kwh'), ('diagnostics.txt','consumption_kwh')]:
        frame = pl.read_csv(tmp_path/name, separator='\t')
        assert frame.height == 25
        times = [datetime.fromisoformat(t) for t in frame['interval_start_utc']]
        assert all((b-a).total_seconds() == 3600 for a,b in zip(times,times[1:]))
        totals.append(frame[energy].sum())
    assert totals == pytest.approx([totals[0]]*3)
    assert json.loads((tmp_path/'run.json').read_text())['summary']['consumption_kwh'] == pytest.approx(totals[0])
    assert (tmp_path/'daily_profile.png').stat().st_size > 0
    assert not (tmp_path/'half_hourly_consumption.txt').exists()
