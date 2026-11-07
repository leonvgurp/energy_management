from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import tomllib

import numpy as np
import polars as pl
import pytest

from energy_management.energy_tables import hourly_energy
from energy_management.regular_usage import COMPONENTS, darkness, generate, generate_pair, shift_summary, validate
from energy_management.regular_usage_reference import assumption_signature, calibrate, model_cells, split_household, summarize_profiles


def settings():
    return tomllib.loads(Path('configs/regular_usage.example.toml').read_text())


@pytest.mark.parametrize('year, hours', [(2026, 8760), (2024, 8784)])
def test_budget_calendar_dst_and_components(year, hours):
    config = settings()
    config['evening_shift']['fraction_moved'] = .7
    config['weekend_shift']['fraction_moved'] = .6
    for frame in generate_pair(config, year, 42):
        validated = hourly_energy(frame, 'energy_kwh', config['timezone'])
        assert frame.height == hours
        assert validated['energy_kwh'].sum() == pytest.approx(2500)
        for name in COMPONENTS:
            assert frame[name].sum() == pytest.approx(2500*config[name]['share'])
        assert np.allclose(frame.select(COMPONENTS).to_numpy().sum(axis=1), frame['energy_kwh'])
        assert (frame['energy_kwh'].to_numpy() >= 0).all()
        dates = frame['interval_start_local'].str.slice(0, 10).to_list()
        if year == 2026:
            assert dates.count('2026-03-29') == 23
            assert dates.count('2026-10-25') == 25


def grouped(frame, component, week=False):
    local = [datetime.fromisoformat(t) for t in frame['interval_start_local']]
    keys = [(t.date()-timedelta(days=t.weekday())).isoformat() if week else t.date().isoformat() for t in local]
    return frame.with_columns(pl.Series('key', keys)).group_by('key').agg(pl.col(component).sum()).sort('key')[component].to_numpy()


@pytest.mark.parametrize('fraction', [0, .5, 1])
def test_shifting_conserves_each_day_and_partial_week(fraction):
    c = settings()
    c['evening_shift']['fraction_moved'] = fraction
    c['weekend_shift']['fraction_moved'] = fraction
    b, s = generate_pair(c, 2026, 17)
    assert np.allclose(grouped(b, 'evening_shift'), grouped(s, 'evening_shift'))
    assert np.allclose(grouped(b, 'weekend_shift', True), grouped(s, 'weekend_shift', True))
    for n in COMPONENTS[:3]:
        assert b[n].equals(s[n])
    if fraction == 0:
        assert b.equals(s)
    if fraction == 1:
        hours = np.array([datetime.fromisoformat(t).hour for t in s['interval_start_local']])
        assert s['evening_shift'].to_numpy()[~np.isin(hours, [12, 13, 14, 15])].sum() == 0
        assert s['weekend_shift'].to_numpy()[~np.isin(hours, [12, 13, 14, 15])].sum() == 0
    assert generate_pair(c, 2026, 17)[1].equals(s)


def test_overlapping_budgets_and_same_realization():
    c = settings()
    b0, s0 = generate_pair(c, 2026, 42)
    c['weekend_shift']['source_hour_weights'] = c['evening_shift']['source_hour_weights']
    c['weekend_shift']['source_day_weights'] = c['evening_shift']['source_day_weights']
    c['evening_shift']['fraction_moved'] = 1
    c['weekend_shift']['fraction_moved'] = 1
    b, s = generate_pair(c, 2026, 42)
    assert b['evening_shift'].equals(b0['evening_shift'])
    assert s['energy_kwh'].sum() == pytest.approx(2500)
    assert not generate_pair(c, 2026, 43)[0].equals(b)
    report = shift_summary(b, s, c)
    assert report.filter(pl.col('component')=='energy_kwh')['net_reallocated_kwh'][0] == pytest.approx(375)


def test_impossible_destination_and_zero_budget():
    c = settings(); c['weekend_shift']['fraction_moved'] = 1
    c['weekend_shift']['destination_day_weights'] = [1,0,0,0,0,0,0]
    # 2023 starts on Sunday: positive source but no Monday within boundary week.
    with pytest.raises(ValueError, match='partial week'):
        generate(c, 2023)
    c['weekend_shift']['share'] = 0
    c['background']['share'] += .05
    assert generate(c, 2023)['energy_kwh'].sum() == pytest.approx(2500)


@pytest.mark.parametrize('key,value', [('weekend_appliance_bias',2), ('evening_shift_bias',1), ('flexible_fraction',.3)])
def test_legacy_rejected(key,value):
    c=settings();c[key]=value
    with pytest.raises(ValueError, match='Legacy.*replace'):
        generate(c,2026)


@pytest.mark.parametrize('section,key,value', [('evening_shift','fraction_moved',1.1),
    ('background','share',float('nan')), ('routine','morning_width',0),
    ('seasonality','amplitude',1), ('evening_shift','source_hour_weights',[1]*23),
    ('variability','samples',-1), ('lighting','bed_hour',3)])
def test_invalid_configuration(section,key,value):
    c=settings();c[section][key]=value
    with pytest.raises(ValueError): validate(c)


def test_no_source_and_annual_scaling():
    c=settings();c['evening_shift']['source_hour_weights']=[0]*24
    with pytest.raises(ValueError, match='source weights'): generate(c,2026)
    c=settings();b=generate(c,2026);c['annual_total_kwh']=5000
    assert np.allclose(generate(c,2026)['energy_kwh'],b['energy_kwh']*2)
    c['annual_total_kwh']=0
    assert generate(c,2026)['energy_kwh'].sum()==0


def test_lighting_is_seasonal_and_occupied():
    c=settings();f=generate(c,2026)
    local=[datetime.fromisoformat(t) for t in f['interval_start_local']]
    h=np.array([t.hour for t in local]);m=np.array([t.month for t in local])
    light=f['lighting'].to_numpy()
    assert light[h<7].sum()==0
    assert light[h>=23].sum()==0
    assert light[m==1].mean()>light[m==7].mean()


def test_calibration_signature_excludes_only_scenario_controls():
    c=settings();signature=assumption_signature(c)
    c['annual_total_kwh']=4000;c['variability']['seed']=23
    c['evening_shift']['fraction_moved']=1;c['weekend_shift']['destination_day_weights']=[1]*7
    assert assumption_signature(c)==signature
    c['background']['share']+=.1
    assert assumption_signature(c)!=signature


def test_household_split_is_stable_and_disjoint():
    assert split_household('liander','Klant 1')==split_household('liander','Klant 1')
    assert {split_household('fluvius',str(i)) for i in range(100)}=={'train','test'}


def test_calibration_never_uses_held_out_values():
    c=settings();target=model_cells(c,2024)
    train=target.with_columns(pl.lit('train').alias('split'),pl.lit('fluvius').alias('dataset'),pl.lit('a').alias('household'))
    test=target.with_columns(pl.lit('test').alias('split'),pl.lit('fluvius').alias('dataset'),pl.lit('b').alias('household'))
    _,fit1,_=calibrate(c,pl.concat([train,test]))
    test=test.with_columns(pl.col('relative_load')*10)
    _,fit2,_=calibrate(c,pl.concat([train,test]))
    assert fit1['parameters']==fit2['parameters']


def test_source_summary_rejects_missing_export_and_invalid_profiles():
    times=pl.datetime_range(datetime(2013,1,1),datetime(2013,12,31,23,45),interval='15m',eager=True)
    frames=[]
    for i in range(13):
        v=np.ones(len(times))*.1
        if i==11:v[0]=np.nan
        f=pl.DataFrame(dict(household=[str(i)]*len(times),time=times,kwh=v,excluded=[i==12]*len(times)))
        if i==10:f=f.slice(1)
        frames.append(f)
    cells,monthly,metrics,quality=summarize_profiles(pl.concat(frames),'fixture',2013,nominal_grid=True)
    assert cells['household'].n_unique()==10
    assert quality.filter(~pl.col('included')).height==3
    assert np.allclose(cells['relative_load'],1)
    shares=monthly.group_by('household').agg(pl.col('annual_share').sum())
    assert np.allclose(shares['annual_share'],1)


def test_liander_export_header_and_wh_conversion(tmp_path, monkeypatch):
    import zipfile
    import energy_management.regular_usage_reference as reference
    times=pl.datetime_range(datetime(2013,1,1),datetime(2013,12,31,23,45),interval='15m',eager=True)
    source=pl.DataFrame({'datetime':times.dt.strftime('%d-%m-%Y %H:%M'),
                         **{f'Klant {i}':np.full(len(times),100) for i in range(11)}})
    with zipfile.ZipFile(tmp_path/'levering.zip','w') as z:
        z.writestr('import.csv',source.write_csv(separator=';'))
    # Regression: totals before header previously prevented all export exclusions.
    export='publisher totals row\ndatetime;Klant 0\n01-01-2013 00:00;1\n'
    with zipfile.ZipFile(tmp_path/'teruglevering.zip','w') as z:
        z.writestr('export.csv',export)
    monkeypatch.setattr(reference,'cached_download',lambda url,path,offline: (path,{'url':url}))
    cells,monthly,metrics,quality,provenance=reference.prepare_liander(tmp_path,True)
    assert cells['household'].n_unique()==10
    assert quality.filter(pl.col('household')=='Klant 0')['included'][0] is False
    assert quality.filter(pl.col('household')=='Klant 1')['annual_kwh'][0]==pytest.approx(3504)
