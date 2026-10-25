from dataclasses import replace
from datetime import date, timedelta

import polars as pl
import pytest

from energy_management.solar import generate_year, hourly_yields, monthly_summary, plot_saved_outputs
from energy_management.solar_history import fit_history
from test_solar import calendar_for


def write_history(path, years):
    rows = []
    for year in years:
        day = date(year, 1, 1)
        while day.year == year:
            # A known two-level daily distribution: 2 or 8 kWh, power max 2 kW.
            energy = 2.0 if day.day % 2 else 8.0
            rows.extend(dict(date_time=f"{day} {hour:02}:00", hourly_yield_kwh=energy/4 if 11 <= hour < 15 else 0.0)
                        for hour in range(24))
            day += timedelta(days=1)
    pl.DataFrame(rows).write_csv(path, separator="\t")


def test_history_fit_empirical_distribution_and_common_outputs(tmp_path):
    path = tmp_path / "history.txt"
    write_history(path, [2023, 2024])
    config, history, source = fit_history(path, "Example")
    assert source["years"] == [2023, 2024]
    assert config.max_power_kw == 2
    assert config.max_daily_kwh == (8.0,)*12
    # February averages a 28-day and 29-day monthly total; no annual input.
    assert config.monthly_kwh[1] == 141
    events = calendar_for(2025)
    empirical = generate_year(config, events, 42, history)
    assert empirical.equals(generate_year(config, events, 42, history))
    assert not empirical.equals(generate_year(config, events, 43, history))
    beta = generate_year(config, events, 42)
    assert not empirical.equals(beta)
    for daily in (empirical, beta):
        for month in range(1, 13):
            assert daily.filter(pl.col("date").dt.month() == month)["daily_kwh"].sum() == pytest.approx(config.monthly_kwh[month-1])
        hourly = hourly_yields(daily, events, config, "Europe/Amsterdam")
        assert hourly.height == 8760
        assert hourly["interval_start_utc"].n_unique() == 8760
        assert hourly["yield_kwh"].sum() == pytest.approx(sum(config.monthly_kwh))
        for name, frame in (("daily_yields", daily), ("monthly_summary", monthly_summary(config, daily)),
                            ("daily_curve_parameters", daily.join(events, on="date").with_columns(
                                pl.lit(config.width).alias("gaussian_width_fraction"),
                                pl.lit(config.max_power_kw).alias("max_power_kw")))):
            frame.write_csv(tmp_path / f"{name}.txt", separator="\t")
        # No history, config or internet needed to render the saved model output.
        plot_saved_outputs(tmp_path)
        assert (tmp_path / "daily_heatmap.png").stat().st_size > 0
        assert (tmp_path / "january_july_curves.png").stat().st_size > 0
    empty = history.filter(pl.col("date").dt.month() != 7)
    with pytest.raises(ValueError, match="cover all months"):
        generate_year(config, events, historical_daily=empty)
    zero = replace(config, monthly_kwh=(0,)*12, max_daily_kwh=(0,)*12)
    assert generate_year(zero, events, historical_daily=history)["daily_kwh"].sum() == 0


def test_history_requires_two_to_five_complete_years(tmp_path):
    path = tmp_path / "history.txt"
    write_history(path, [2023])
    with pytest.raises(ValueError, match="2–5"):
        fit_history(path, "Example")
    write_history(path, [2023, 2024])
    pl.read_csv(path, separator="\t").slice(1).write_csv(path, separator="\t")
    with pytest.raises(ValueError, match="complete calendar years"):
        fit_history(path, "Example")
