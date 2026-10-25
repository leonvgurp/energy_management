from datetime import date, datetime, timedelta, timezone
import tomllib
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from energy_management.boiler import simulate
from energy_management.energy_tables import hourly_energy, write_energy_tables


@pytest.mark.parametrize('day,expected_hours', [(date(2026,3,29),23), (date(2026,10,25),25)])
def test_component_contract_dst_and_energy(tmp_path, day, expected_hours):
    with open('configs/boiler.example.toml', 'rb') as f:
        config = tomllib.load(f)
    boiler = simulate(config, day, day+timedelta(days=1))
    hourly, daily = write_energy_tables(boiler, tmp_path/'boiler', 'consumption_kwh', 'Europe/Amsterdam', 30)
    assert hourly.height == expected_hours
    assert hourly.columns == ['interval_start_utc', 'interval_start_local', 'energy_kwh']
    assert daily.columns == ['date', 'energy_kwh']
    assert hourly['energy_kwh'].sum() == pytest.approx(boiler['consumption_kwh'].sum())
    assert daily['energy_kwh'].sum() == pytest.approx(hourly['energy_kwh'].sum())
    # The two existing component interfaces must produce precisely the same contract.
    for name in ('yield_kwh', 'consumption_kwh'):
        source = hourly.rename({'energy_kwh': name})
        result = hourly_energy(source, name, 'Europe/Amsterdam')
        assert result.equals(hourly)
    if expected_hours == 25:
        repeated = hourly.filter(pl.col('interval_start_local').str.slice(11, 2) == '02')
        assert repeated.height == 2
        assert repeated['interval_start_local'].n_unique() == 2


def readings():
    start = datetime(2026,1,1,tzinfo=timezone.utc)
    zone = ZoneInfo('Europe/Amsterdam')
    return pl.DataFrame([dict(interval_start_utc=(start+timedelta(minutes=30*i)).isoformat(),
                             interval_start_local=(start+timedelta(minutes=30*i)).astimezone(zone).isoformat(),
                             energy_kwh=.25) for i in range(4)])


@pytest.mark.parametrize('indices', [[0,2,3], [0,1,1,3], [1,0,2,3], [0,1,2]])
def test_missing_duplicate_unordered_or_partial_hours(indices):
    with pytest.raises(ValueError):
        hourly_energy(readings()[indices], 'energy_kwh', 'Europe/Amsterdam', 30)


def test_wrong_local_offset_and_invalid_energy():
    frame = readings()
    with pytest.raises(ValueError):
        hourly_energy(frame.with_columns(pl.lit('2026-01-01T00:00:00').alias('interval_start_local')), 'energy_kwh', 'Europe/Amsterdam', 30)
    for value in (None, float('nan'), -1):
        with pytest.raises(ValueError):
            hourly_energy(frame.with_columns(pl.lit(value).alias('energy_kwh')), 'energy_kwh', 'Europe/Amsterdam', 30)
