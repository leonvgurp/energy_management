from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from energy_management.solar import SolarConfig, generate_year, hourly_yields


@pytest.fixture
def config():
    return SolarConfig("Example", (100, 150, 250, 350, 500, 550, 530, 480, 350, 220, 140, 90),
                       (15, 18, 22, 25, 30, 30, 30, 28, 25, 22, 18, 15), 3.85)


def calendar_for(year):
    start = date(year, 1, 1)
    dates = [start+timedelta(days=i) for i in range((date(year+1, 1, 1)-start).days)]
    return pl.DataFrame(dict(date=dates, sunrise_hour=[7.0]*len(dates),
                             solar_noon_hour=[13.0]*len(dates), sunset_hour=[19.0]*len(dates)))


@pytest.mark.parametrize("year", [2024, 2025])
def test_seeded_scenarios_conserve_monthly_budgets(config, year):
    events = calendar_for(year)
    a, b = generate_year(config, events, 42), generate_year(config, events, 43)
    assert a.equals(generate_year(config, events, 42))
    assert not a.equals(b)
    assert a.height == events.height
    for month in range(1, 13):
        for daily in (a, b):
            values = daily.filter(pl.col("date").dt.month() == month)["daily_kwh"]
            assert values.sum() == pytest.approx(config.monthly_kwh[month-1], abs=1e-8)
            assert values.min() >= 0
            assert values.max() <= config.max_daily_kwh[month-1]
    assert a["daily_kwh"].sum() == pytest.approx(sum(config.monthly_kwh))


def test_config_load_and_invalid_inputs(config, tmp_path):
    from dataclasses import asdict
    path = tmp_path / "solar.toml"
    values = asdict(config)
    path.write_text("\n".join(f"{key} = {repr(list(value) if isinstance(value, tuple) else value)}"
                              for key, value in values.items() if value is not None))
    assert SolarConfig.load(path) == config
    for changes in (dict(monthly_kwh=(1,)*11), dict(max_daily_kwh=(-1,)*12),
                    dict(max_power_kw=0), dict(width=float("nan")),
                    dict(beta_concentration=0), dict(latitude=52), dict(locality="")):
        with pytest.raises(ValueError):
            replace(config, **changes)


def test_infeasible_caps_and_missing_calendar_rejected(config):
    events = calendar_for(2025)
    with pytest.raises(ValueError, match="infeasible"):
        generate_year(replace(config, monthly_kwh=(1000,)*12, max_daily_kwh=(1,)*12), events)
    with pytest.raises(ValueError, match="exactly once"):
        generate_year(config, events.slice(1))
    with pytest.raises(ValueError, match="infeasible"):
        generate_year(replace(config, max_power_kw=0.01), events)


def test_zero_month_and_degenerate_upper_bound(config):
    events = calendar_for(2025)
    zero = replace(config, monthly_kwh=(0,)+config.monthly_kwh[1:], max_daily_kwh=(0,)+config.max_daily_kwh[1:])
    daily = generate_year(zero, events)
    assert daily.filter(pl.col("date").dt.month() == 1)["daily_kwh"].sum() == 0
    totals = tuple(events.filter(pl.col("date").dt.month() == m).height*10 for m in range(1, 13))
    flat = generate_year(replace(config, monthly_kwh=totals, max_daily_kwh=(10,)*12), events)
    assert flat["daily_kwh"].to_list() == [10]*365


def test_hourly_output_handles_dst_and_preserves_daily_energy(config):
    events = calendar_for(2024)
    daily = generate_year(config, events)
    hourly = hourly_yields(daily, events, config, "Europe/Amsterdam")
    assert hourly.height == 8784
    assert hourly["interval_start_utc"].n_unique() == hourly.height
    groups = hourly.with_columns(pl.col("interval_start_local").str.slice(0, 10).alias("date"))
    counts = groups.group_by("date").len()
    assert counts.filter(pl.col("date") == "2024-03-31")["len"][0] == 23
    assert counts.filter(pl.col("date") == "2024-10-27")["len"][0] == 25
    totals = groups.group_by("date").agg(pl.col("yield_kwh").sum()).sort("date")
    np.testing.assert_allclose(totals["yield_kwh"], daily["daily_kwh"], rtol=0, atol=1e-7)
    assert hourly["yield_kwh"].max() <= config.max_power_kw
