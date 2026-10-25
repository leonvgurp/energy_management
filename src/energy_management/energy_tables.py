"""Shared component energy-table contract: positive kWh per elapsed hour."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import math

import polars as pl


def hourly_energy(frame, value_column, timezone_name, interval_minutes=60):
    """Validate complete input intervals and sum them into UTC-aligned hours.

    Never average energy, fill missing readings, or aggregate offset-free labels.
    """
    if interval_minutes not in (30, 60) or frame.is_empty():
        raise ValueError('Require nonempty 30- or 60-minute intervals')
    zone = ZoneInfo(timezone_name)
    step = timedelta(minutes=interval_minutes)
    times, values = [], []
    for row in frame.iter_rows(named=True):
        utc = datetime.fromisoformat(row['interval_start_utc'])
        local = datetime.fromisoformat(row['interval_start_local'])
        value = row[value_column]
        if utc.utcoffset() != timedelta(0) or local.utcoffset() is None:
            raise ValueError('Require explicit UTC and offset-aware local timestamps')
        utc = utc.astimezone(timezone.utc)
        expected = utc.astimezone(zone)
        if local.replace(tzinfo=None) != expected.replace(tzinfo=None) or local.utcoffset() != expected.utcoffset():
            raise ValueError('Local timestamp does not match UTC and configured timezone')
        if utc.second or utc.microsecond or utc.minute % interval_minutes:
            raise ValueError('Input timestamps must align to interval boundaries')
        if times and utc - times[-1] != step:
            raise ValueError('Missing, duplicate or unordered intervals')
        if value is None or not math.isfinite(value) or value < 0:
            raise ValueError('Energy must be finite, nonnegative kWh')
        times.append(utc)
        values.append(float(value))
    if times[0].minute or (times[-1]+step).minute:
        raise ValueError('Require complete elapsed hours')
    count = 60 // interval_minutes
    return pl.DataFrame([
        dict(interval_start_utc=times[i].isoformat(),
             interval_start_local=times[i].astimezone(zone).isoformat(),
             energy_kwh=sum(values[i:i+count]))
        for i in range(0, len(times), count)
    ])


def write_energy_tables(frame, output, value_column, timezone_name, interval_minutes=60):
    """Write identical hourly and local-calendar daily schemas for every component."""
    hourly = hourly_energy(frame, value_column, timezone_name, interval_minutes)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    hourly.write_csv(output/'hourly_energy.txt', separator='\t')
    daily = hourly.with_columns(pl.col('interval_start_local').str.slice(0, 10).alias('date')).group_by('date', maintain_order=True).agg(pl.col('energy_kwh').sum())
    daily.write_csv(output/'daily_energy.txt', separator='\t')
    return hourly, daily
