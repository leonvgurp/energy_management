from datetime import datetime, timedelta
import json
from pathlib import Path
import tomllib

import numpy as np
import polars as pl
import pytest

from energy_management.heat_pump_weather import bounds
from energy_management.weather_scenarios import sample_weather, reference_config, daily_weather, generate, load_config, longest_spell, phase
from energy_management.heat_pump import load_config as load_heat_config, simulate


def config(year=2024):
    c = tomllib.loads(Path('configs/weather_scenarios.example.toml').read_text())
    c['weather']['years'] = [year]
    c['sampling'].update(reference_years=[2019, 2020], members=2)
    return c


def history(c):
    start, end = bounds(reference_config(c))
    times = [start+timedelta(hours=i) for i in range(int((end-start).total_seconds()/3600))]
    t = np.array([8-8*np.cos(2*np.pi*phase(d.date())/366)+2*np.sin(2*np.pi*d.hour/24) for d in times])
    return pl.DataFrame(dict(interval_start_utc=[d.isoformat() for d in times], outdoor_c=t, humidity_pct=80-t, wind_m_s=2+t/20, radiation_kwh_m2=[max(0, np.sin(np.pi*(d.hour-6)/12))*.4 for d in times]))


def test_sampler_deterministic_seasonal_joint_blocks_and_dst():
    c = config()
    h = history(c)
    first, blocks = sample_weather(h, c, 42)
    same, same_blocks = sample_weather(h, c, 42)
    other, _ = sample_weather(h, c, 43)
    assert first.equals(same) and blocks == same_blocks
    assert not first.equals(other)
    assert first.height == 366*24
    assert first['interval_start_utc'].n_unique() == first.height
    d = daily_weather(first, c['weather']['timezone'])
    assert d.filter(pl.col('date') == '2024-03-31')['hours'][0] == 23
    assert d.filter(pl.col('date') == '2024-10-27')['hours'][0] == 25
    assert d.height == 366
    source = h.rename({'interval_start_utc': 'source_interval_start_utc'})
    paired = first.join(source, on='source_interval_start_utc', suffix='_source')
    for key in ['outdoor_c', 'humidity_pct', 'wind_m_s', 'radiation_kwh_m2']:
        assert np.array_equal(paired[key], paired[key+'_source'])
    for block in blocks:
        diff = abs(phase(datetime.fromisoformat(block['target_start_utc']).date())-phase(datetime.fromisoformat(block['source_start_utc']).date()))
        assert min(diff, 366-diff) <= c['sampling']['seasonal_window_days']
        if block['join_temperature_change_c'] is not None:
            assert abs(block['join_temperature_change_c']) <= c['sampling']['max_join_temperature_jump_c']
        part = first.filter(pl.col('block_id') == block['block_id'])
        stamps = [datetime.fromisoformat(x) for x in part['source_interval_start_utc']]
        assert all(b-a == timedelta(hours=1) for a, b in zip(stamps, stamps[1:]))


def test_invalid_history_and_no_eligible_blocks():
    c = config()
    h = history(c)
    invalid = config()
    invalid['sampling']['block_days'] = 0
    with pytest.raises(ValueError, match='block_days'):
        sample_weather(h, invalid, 42)
    with pytest.raises(ValueError, match='Missing'):
        sample_weather(h.head(h.height-1), c, 42)
    # With no seasonal window, Feb 29 cannot be sourced from a nonleap year.
    c['sampling'].update(reference_years=[2019], seasonal_window_days=0, block_days=1)
    c['weather'].update(start='2024-02-29', end='2024-03-01')
    with pytest.raises(ValueError, match='No source block'):
        sample_weather(history(c), c, 42)


def test_sampling_changes_with_block_config():
    c = config()
    h = history(c)
    one, blocks = sample_weather(h, c, 42)
    c['sampling']['block_days'] = 14
    two, fewer_blocks = sample_weather(h, c, 42)
    assert len(fewer_blocks) < len(blocks)
    assert not one.equals(two)
    assert longest_spell([2, -1, -2, 1, -3], 0) == 2


def test_saved_synthetic_contract_and_heat_pump(tmp_path):
    # Use a full target year so seasonal diagnostic plots are meaningful.
    text = Path('configs/weather_scenarios.example.toml').read_text().replace('years = [2027]', 'years = [2024]').replace('reference_years = [2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]', 'reference_years = [2019, 2020]').replace('members = 30', 'members = 2')
    cfg = tmp_path/'sampling.toml'; cfg.write_text(text)
    c = load_config(cfg)
    directory = tmp_path/'history'; directory.mkdir()
    history(c).write_csv(directory/'weather.txt', separator='\t')
    (directory/'weather.json').write_text(json.dumps(dict(provider='KNMI observations', station=260, timezone='Europe/Amsterdam')))
    root = tmp_path/'out'
    report = generate(cfg, directory, root)
    assert report['maximum_join_temperature_change_c'] <= 3
    member = root/'member_000'
    metadata = json.loads((member/'weather.json').read_text())
    assert metadata['synthetic'] and metadata['seed'] == 42
    assert metadata['source_weather_sha256']
    assert (root/'weather_validation.png').stat().st_size > 1000
    f = pl.read_csv(member/'weather.txt', separator='\t')
    model = load_heat_config('configs/heat_pump.example.toml')
    model['weather'] = c['weather']
    result = simulate(f, model)
    assert result.height == f.height
    assert result['consumption_kwh'].min() >= 0
    # Observed input remains unchanged, and model no longer needs historical dates.
    assert pl.read_csv(directory/'weather.txt', separator='\t').height == history(c).height


def test_invalid_sampling_config(tmp_path):
    text = Path('configs/weather_scenarios.example.toml').read_text().replace('block_days = 7', 'block_days = 0')
    cfg = tmp_path/'invalid.toml'; cfg.write_text(text)
    with pytest.raises(ValueError, match='block_days'):
        load_config(cfg)
