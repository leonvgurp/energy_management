from datetime import date, datetime

import numpy as np
import pytest

from energy_management.solar_comparison import (
    accuracy_metrics, hourly_model_energy, interval_starts_local,
    select_monthly_peak_days,
)


def test_hourly_integrals_conserve_daily_energy_and_respect_power_cap():
    energy = hourly_model_energy(np.arange(24), 33.35, 5.4, 13.7, 22, max_power_kw=3.85)
    assert energy.sum() == pytest.approx(33.35, abs=1e-7)
    assert energy.min() >= 0
    assert energy.max() <= 3.85
    assert energy[:5] == pytest.approx(np.zeros(5))
    assert energy[22:] == pytest.approx(np.zeros(2))
    # A partial daylight hour must integrate to less than a full hour at the cap.
    assert energy[5] < 0.6 * 3.85


def test_hourly_metrics_have_known_values():
    result = accuracy_metrics([0, 2], [1, 1])
    assert result['mae_kwh'] == 1
    assert result['rmse_kwh'] == 1
    assert result['r_squared'] == 0
    assert result['wape_percent'] == 100
    assert result['measured_total_kwh'] == result['modeled_total_kwh'] == 2
    zero = accuracy_metrics([0, 0], [0, 0])
    assert zero['wape_percent'] is None and zero['r_squared'] is None


@pytest.mark.parametrize('measured,modeled', [([], []), ([1], [1, 2]), ([-1], [1]), ([1], [float('nan')])])
def test_bad_metric_input_rejected(measured, modeled):
    with pytest.raises(ValueError):
        accuracy_metrics(measured, modeled)


def test_timestamp_semantics_are_explicit():
    day = date(2023, 7, 7)
    labels = [datetime(2023, 7, 7, h) for h in (0, 12, 23)]
    assert interval_starts_local(labels, day).tolist() == [0, 12, 23]
    assert interval_starts_local(labels, day, 'Europe/Amsterdam', 'end').tolist() == [-1, 11, 22]
    assert interval_starts_local(labels, day, 'UTC', 'start').tolist() == [2, 14, 25]
    assert interval_starts_local(labels, day, 'UTC', 'end').tolist() == [1, 13, 24]


def test_ambiguous_or_missing_dst_labels_rejected():
    for stamp in [datetime(2024, 3, 31, 2), datetime(2024, 10, 27, 2)]:
        with pytest.raises(ValueError, match='DST'):
            interval_starts_local([stamp], stamp.date())


def test_monthly_peak_selection_uses_all_years_and_breaks_ties_by_date():
    import polars as pl
    rows = []
    for month in range(1, 13):
        rows.extend([dict(date=date(2023, month, 1), daily_kwh=10.0),
                     dict(date=date(2023, month, 2), daily_kwh=20.0),
                     dict(date=date(2024, month, 1), daily_kwh=21.0 if month == 3 else 20.0)])
    selected = select_monthly_peak_days(pl.DataFrame(rows).reverse())
    assert selected['month'].to_list() == list(range(1, 13))
    assert selected['date'].to_list() == [date(2024, 3, 1) if m == 3 else date(2023, m, 2)
                                         for m in range(1, 13)]
    with pytest.raises(ValueError, match='twelve'):
        select_monthly_peak_days(pl.DataFrame(rows).filter(pl.col('date').dt.month() != 12))
