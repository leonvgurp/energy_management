from datetime import date, datetime, timedelta

import numpy as np
import polars as pl
import pytest
from scipy.integrate import quad

from energy_management.solar import gaussian_power, gaussian_interval_energy
from energy_management.solar_comparison import read_daily_yields, read_solar_calendar


@pytest.mark.parametrize("energy", [0, 2.5, 34.15])
@pytest.mark.parametrize("width", [0.1, 0.45, 1.5])
def test_curve_integral_peak_and_daylight(energy, width):
    rise, noon, setting = 8.1, 12.7, 16.9
    fn = lambda h: float(gaussian_power(h, energy, rise, noon, setting, width, max_power_kw=None))
    area = quad(fn, 0, 24, points=[rise, noon, setting], epsabs=1e-10)[0]
    assert area == pytest.approx(energy, abs=1e-8)
    assert np.array_equal(gaussian_power([0, rise, setting, 24], energy, rise, noon, setting, width,
                                       max_power_kw=None), np.zeros(4))
    assert fn(noon) >= fn(noon - 0.01)
    assert fn(noon) >= fn(noon + 0.01)
    assert fn(noon) >= 0


def test_energy_power_units():
    # A symmetric curve with twice the daylight duration has half the peak power.
    a = gaussian_power(12, 10, 8, 12, 16)
    b = gaussian_power(12, 10, 4, 12, 20)
    assert a == pytest.approx(2 * b)


def test_daily_adapter_missing_and_duplicate_readings(tmp_path):
    start = datetime(2023, 1, 1)
    stamps = [start + timedelta(hours=i) for i in range(365 * 24)]
    data = pl.DataFrame({"date_time": [t.strftime("%Y-%m-%d %H:%M") for t in stamps],
                         "hourly_yield_kwh": np.ones(len(stamps))})
    path = tmp_path / "hourly.txt"
    data.write_csv(path, separator="\t")
    daily = read_daily_yields(path)
    assert daily.height == 365
    assert daily["daily_kwh"].to_list() == [24] * 365
    for bad in (data.slice(1), data.filter(pl.int_range(pl.len()) != 100),
                pl.concat([data, data.slice(100, 1)]),
                data.with_columns(pl.lit(float("nan")).alias("hourly_yield_kwh"))):
        bad.write_csv(path, separator="\t")
        with pytest.raises(ValueError):
            read_daily_yields(path)


def test_solar_calendar_leap_and_dst_boundaries(tmp_path):
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("Europe/Amsterdam")
    rows = []
    for i in range(366):
        day = date(2024, 1, 1) + timedelta(days=i)
        rows.append(dict(date=day.isoformat(), timezone="Europe/Amsterdam",
                         sunrise_local=datetime(day.year, day.month, day.day, 8, tzinfo=zone).isoformat(),
                         solar_noon_local=datetime(day.year, day.month, day.day, 12, tzinfo=zone).isoformat(),
                         sunset_local=datetime(day.year, day.month, day.day, 17, tzinfo=zone).isoformat()))
    path = tmp_path / "solar.txt"
    pl.DataFrame(rows).write_csv(path, separator="\t")
    calendar = read_solar_calendar(path, 2024)
    assert calendar.height == 366
    assert date(2024, 2, 29) in calendar["date"].to_list()
    for day in [date(2024, 3, 31), date(2024, 10, 27)]:
        assert calendar.filter(pl.col("date") == day)["sunrise_hour"][0] == 8
    rows[0]["sunrise_local"] = "2024-01-01T08:00:00+02:00"
    pl.DataFrame(rows).write_csv(path, separator="\t")
    with pytest.raises(ValueError, match="timezone offset"):
        read_solar_calendar(path, 2024)


@pytest.mark.parametrize("energy", [0, 2.5, 25, 33])
@pytest.mark.parametrize("width", [0.1, 0.45, 1.5])
def test_capped_curve_preserves_energy_and_daylight(energy, width):
    rise, noon, setting, cap = 8.1, 12.7, 16.9, 3.85
    # Integrate sampled power independently of the analytical solver, including
    # the nonsmooth transitions into and out of the plateau.
    hours = np.linspace(rise, setting, 100001)
    power = gaussian_power(hours, energy, rise, noon, setting, width, cap)
    assert np.trapezoid(power, hours) == pytest.approx(energy, abs=2e-7)
    assert power.min() >= 0
    assert power.max() <= cap
    assert gaussian_power([0, rise, setting, 24], energy, rise, noon, setting, width, cap) == pytest.approx([0]*4)


def test_capping_creates_plateau_and_raises_shoulders():
    hours = np.array([7, 11, 12, 13, 17])
    ordinary = gaussian_power(hours, 30, 6, 12, 18, max_power_kw=None)
    capped = gaussian_power(hours, 30, 6, 12, 18, max_power_kw=3)
    assert capped[1:4] == pytest.approx([3, 3, 3])
    assert capped[0] > ordinary[0]
    assert capped[-1] > ordinary[-1]


def test_low_yield_curve_unchanged_by_cap():
    hours = np.linspace(0, 24, 1000)
    capped = gaussian_power(hours, 5, 6, 12, 18, max_power_kw=3.85)
    uncapped = gaussian_power(hours, 5, 6, 12, 18, max_power_kw=None)
    np.testing.assert_array_equal(capped, uncapped)


@pytest.mark.parametrize("energy", [24, 25])
def test_impossible_daylight_capacity_rejected(energy):
    # Equality is also impossible for a continuous curve that is zero at endpoints.
    with pytest.raises(ValueError, match="infeasible"):
        gaussian_power([12], energy, 8, 12, 16, max_power_kw=3)


@pytest.mark.parametrize("cap", [0, -1, float("nan"), float("inf")])
def test_invalid_power_ceiling_rejected(cap):
    with pytest.raises(ValueError, match="max_power_kw"):
        gaussian_power([12], 1, 8, 12, 16, max_power_kw=cap)


def test_curve_just_below_capacity():
    from energy_management.solar import _power_amplitude
    energy, daylight, cap = 23.999, 8, 3
    amplitude = _power_amplitude(energy, daylight, 0.45, cap)
    # Resolve the tiny shoulders explicitly when integrating this extreme case.
    b = np.exp(-0.5 / 0.45**2)
    u = np.sqrt(-2 * 0.45**2 * np.log(b + (1-b) * cap / amplitude))
    points = [8, 12 - 4*u, 12, 12 + 4*u, 16]
    area = quad(lambda t: float(gaussian_power(t, energy, 8, 12, 16, max_power_kw=cap)),
                8, 16, points=points, epsabs=1e-9)[0]
    assert area == pytest.approx(energy, abs=1e-8)
