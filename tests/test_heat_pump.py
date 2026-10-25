from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from energy_management.heat_pump import load_config, simulate, flow_target, run, reference_comparisons
from energy_management.heat_pump_weather import bounds, parse_knmi, validate_weather, download_weather
from energy_management.heat_pump_plots import plot_saved_outputs


def config(start='2025-01-01', end='2025-01-04'):
    c = load_config('configs/heat_pump.example.toml')
    c['weather'].update(start=start, end=end)
    return c


def weather(c, temperature=0):
    start, end = bounds(c)
    count = int((end-start).total_seconds()/3600)
    return pl.DataFrame(dict(interval_start_utc=[(start+timedelta(hours=i)).isoformat() for i in range(count)], outdoor_c=[float(temperature)]*count, humidity_pct=[80.0]*count, wind_m_s=[2.0]*count, radiation_kwh_m2=[0.0]*count))


def test_knmi_units_end_labels_and_missing():
    header = '# STN,YYYYMMDD,HH,T,U,FH,Q\n'
    f = parse_knmi(header+'260,20250101,24,25,80,30,360\n', 260)
    assert f['interval_start_utc'][0] == '2025-01-01T23:00:00+00:00'
    assert f['outdoor_c'][0] == 2.5
    assert f['wind_m_s'][0] == 3
    assert f['radiation_kwh_m2'][0] == 1
    for row in ['260,20250101,1,,80,30,0', '260,20250101,0,25,80,30,0', '260,20250101,1,25,101,30,0']:
        with pytest.raises(ValueError):
            parse_knmi(header+row, 260)


def test_accounting_steady_state_and_baseline():
    c = config()
    c['house'].update(heat_loss_kw_per_k=.1, internal_gains_kw=0, solar_aperture_m2=0)
    c['operation']['emitter_kw_per_k'] = 10
    c['operation']['initial_heating_kw'] = 2
    f = simulate(weather(c), c)
    # H=0.1 kW/K, 20K differential -> 2kWh heat per elapsed hour.
    assert np.allclose(f['delivered_heat_kwh'], 2)
    assert np.allclose(f['indoor_end_c'], 20)
    assert np.allclose(f['consumption_kwh'], f['delivered_heat_kwh']/f['cop']+f['auxiliary_kwh'])
    assert np.allclose(f['delivered_heat_kwh']+f['gains_kwh'], f['storage_change_kwh']+f['loss_kwh'])
    assert np.allclose(f['baseline_consumption_kwh'], f['consumption_kwh'])


def test_capacity_flow_shutdown_and_thermal_lag():
    c = config()
    assert flow_target(50, c['house'], c['operation']) == 24
    assert flow_target(-100, c['house'], c['operation']) == 42
    c['operation']['capacity_multiplier'] = .01
    f = simulate(weather(c, -15), c)
    assert f['capacity_shortfall_kwh'].min() > 0
    assert f['unmet_heat_kwh'].sum() > 0
    assert f['performance_boundary'].all()
    assert f['indoor_end_c'][0] < f['indoor_start_c'][0]
    summer = config('2025-06-01', '2025-06-03')
    s = simulate(weather(summer, 5), summer)
    assert s['delivered_heat_kwh'].sum() == 0
    assert s['consumption_kwh'].sum() == 0
    assert s['indoor_end_c'][0] > 5  # finite thermal storage, not instant outdoor equilibrium
    warm = simulate(weather(config(), 25), config())
    assert warm['consumption_kwh'].sum() == 0


@pytest.mark.parametrize('start,end,count', [('2024-03-31','2024-04-01',23), ('2024-10-27','2024-10-28',25), ('2024-02-28','2024-03-01',48)])
def test_dst_and_leap(start, end, count):
    c = config(start, end)
    w = weather(c)
    f = simulate(w, c)
    assert f.height == count
    assert f['interval_start_utc'].n_unique() == count
    assert f['interval_start_local'].n_unique() == count
    with pytest.raises(ValueError, match='Missing'):
        simulate(w.head(count-1), c)
    duplicate = pl.concat([w.head(1), w.head(count-1)])
    with pytest.raises(ValueError, match='Missing'):
        simulate(duplicate, c)


def test_sensitivity_and_reproducibility():
    c = config()
    w = weather(c)
    base = simulate(w, c)
    assert base.equals(simulate(w, c))
    altered = deepcopy(c)
    altered['house']['heat_loss_kw_per_k'] *= 1.2
    assert simulate(w, altered)['consumption_kwh'].sum() > base['consumption_kwh'].sum()
    assert simulate(w, c, 'low')['consumption_kwh'].sum() < base['consumption_kwh'].sum()
    assert simulate(w, c, 'high')['consumption_kwh'].sum() > base['consumption_kwh'].sum()
    altered['house']['heat_loss_kw_per_k'] = float('nan')
    with pytest.raises(ValueError):
        simulate(w, altered)


def test_cache_offline(tmp_path, monkeypatch):
    import energy_management.heat_pump_weather as module
    c = config('2025-01-01', '2025-01-02')
    def fake(request, timeout):
        # adapter requests both UTC dates straddling the local day
        # KNMI can include the next boundary day; overlapping request tails
        # must be trimmed, without hiding duplicates inside requested coverage.
        days = ['20241231', '20250101'] if 'start=20241231' in request.full_url else ['20250101', '20250102']
        raw = '# STN,YYYYMMDD,HH,T,U,FH,Q\n'+'\n'.join(f'260,{day},{h},25,80,30,0' for day in days for h in range(1,25))
        return io.BytesIO(raw.encode())
    monkeypatch.setattr(module, 'urlopen', fake)
    first = download_weather(c, tmp_path/'weather', tmp_path/'cache')
    monkeypatch.setattr(module, 'urlopen', lambda *args, **kw: pytest.fail('Network used offline'))
    assert first.equals(download_weather(c, tmp_path/'weather', tmp_path/'cache', True))
    with pytest.raises(ValueError, match='cached'):
        download_weather(c, tmp_path/'weather', tmp_path/'empty', True)


def test_saved_outputs_references_and_plots(tmp_path):
    c = config('2025-01-01', '2025-01-08')
    # Reuse TOML serialization by editing the public fixture.
    text = Path('configs/heat_pump.example.toml').read_text().replace('start = ""', 'start = "2025-01-01"').replace('end = ""', 'end = "2025-01-08"')
    cfg = tmp_path/'config.toml'; cfg.write_text(text)
    wd = tmp_path/'weather'; wd.mkdir()
    weather(c).write_csv(wd/'weather.txt', separator='\t')
    (wd/'weather.json').write_text(json.dumps(dict(station=260, timezone='Europe/Amsterdam')))
    out = tmp_path/'out'
    run(cfg, wd, out)
    before = (out/'base/hourly_consumption.txt').read_bytes()
    run(cfg, wd, out)
    assert before == (out/'base/hourly_consumption.txt').read_bytes()
    f = pl.read_csv(out/'base/hourly_consumption.txt', separator='\t')
    d = pl.read_csv(out/'base/daily_consumption.txt', separator='\t')
    m = pl.read_csv(out/'base/monthly_summary.txt', separator='\t')
    assert f['consumption_kwh'].sum() == pytest.approx(d['daily_kwh'].sum())
    assert f['consumption_kwh'].sum() == pytest.approx(m['consumption_kwh'].sum())
    combined = pl.concat([simulate(weather(c), c, s) for s in ('low','base','high')])
    ref = dict(observations=[dict(label='test', start='2025-01-01', end='2025-01-08', electricity_kwh=100, environmental_yield_kwh=300, scope='test', uncertainty='unknown')])
    comparison = reference_comparisons(combined, ref)[0]
    assert comparison['implied_heat_kwh'] == 400
    assert comparison['implied_period_cop'] == 4
    assert comparison['difference_kwh'] != 0  # references never rescale
    plot_saved_outputs(out)
    for name in ('daily_heatmap_2025.png','winter_days.png','winter_profiles.png','scenario_comparison.png'):
        assert (out/name).stat().st_size > 1000


def test_explicit_response_lag():
    c = config()
    c['operation']['emitter_kw_per_k'] = 10
    slow = simulate(weather(c), c)
    c['operation']['response_hours'] = .1
    fast = simulate(weather(c), c)
    assert slow['delivered_heat_kwh'][0] < fast['delivered_heat_kwh'][0]
    assert slow['indoor_end_c'][0] < fast['indoor_end_c'][0]
