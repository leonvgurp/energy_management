"""Fit monthly budgets, caps and empirical distributions from 2–5 complete years."""

import hashlib
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from .solar import SolarConfig, main
from .solar_comparison import read_daily_yields


def fit_history(path, locality, country_code=None, max_power_kw=None,
                input_timezone="Europe/Amsterdam"):
    ZoneInfo(input_timezone)
    path = Path(path)
    daily = read_daily_yields(path)
    dated = daily.with_columns(pl.col("date").dt.year().alias("year"),
                               pl.col("date").dt.month().alias("month"))
    years = sorted(dated["year"].unique().to_list())
    if not 2 <= len(years) <= 5:
        raise ValueError("Provide 2–5 complete calendar years of hourly yields")
    totals = dated.group_by("year", "month").agg(pl.col("daily_kwh").sum())
    means = totals.group_by("month").agg(pl.col("daily_kwh").mean()).sort("month")
    maxima = dated.group_by("month").agg(pl.col("daily_kwh").max()).sort("month")
    measured_cap = pl.read_csv(path, separator="\t")["hourly_yield_kwh"].max()
    cap = measured_cap if max_power_kw is None else max_power_kw
    config = SolarConfig(locality, tuple(means["daily_kwh"]), tuple(maxima["daily_kwh"]),
                         cap, country_code=country_code)
    source = dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                  years=years, first_date=str(daily["date"][0]), last_date=str(daily["date"][-1]),
                  resolution="hourly energy (kWh)", input_timezone_assumption=input_timezone,
                  interval_label_assumption="start", timestamp_convention="naive regular 24-label calendar days",
                  dst_limitation="Source DST labels unresolved; source energy grouped by supplied date, without assigning UTC instants.",
                  power_cap_source="measured hourly-average maximum (provisional instantaneous cap)" if max_power_kw is None else "user supplied")
    return config, daily, source


if __name__ == "__main__":
    main("history")
