"""Compare measured high-yield days with the simple capped Gaussian model."""

from __future__ import annotations

import argparse
import calendar
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
import polars as pl

from .solar import gaussian_interval_energy, gaussian_power


def read_daily_yields(path: str | Path) -> pl.DataFrame:
    """Validate this export's naive hourly labels, then sum energy by labeled day.

    No timezone is inferred or attached. This adapter requires complete calendar
    years of regular naive 24-hour days, as in the supplied export. Offset-aware
    or irregular DST exports require a different explicit ingestion convention.
    """
    frame = pl.read_csv(path, separator="\t", schema_overrides={
        "date_time": pl.String, "hourly_yield_kwh": pl.Float64})
    frame = frame.select(
        pl.col("date_time").str.strptime(pl.Datetime, "%Y-%m-%d %H:%M", strict=True),
        "hourly_yield_kwh",
    ).sort("date_time")
    if frame.is_empty() or frame.null_count().row(0) != (0, 0):
        raise ValueError("Hourly input must contain nonmissing timestamps and yields")
    energy = frame["hourly_yield_kwh"].to_numpy()
    if not np.isfinite(energy).all() or (energy < 0).any():
        raise ValueError("Hourly yields must be finite and nonnegative")
    times = frame["date_time"].to_list()
    if any(t.minute or t.second for t in times):
        raise ValueError("Readings must be aligned to whole hours")
    if any(b - a != timedelta(hours=1) for a, b in zip(times, times[1:])):
        raise ValueError("Missing or duplicate hourly labels; resolve timezone/DST first")
    if times[0] != datetime(times[0].year, 1, 1) or times[-1] != datetime(times[-1].year, 12, 31, 23):
        raise ValueError("Fit requires complete calendar years, without partial months")
    return frame.group_by(pl.col("date_time").dt.date().alias("date")).agg(
        pl.col("hourly_yield_kwh").sum().alias("daily_kwh")
    ).sort("date")


def read_solar_calendar(path: str | Path, year: int) -> pl.DataFrame:
    """Parse offset-aware local solar events, rejecting inconsistent dates/offsets."""
    from zoneinfo import ZoneInfo

    source = pl.read_csv(path, separator="\t", try_parse_dates=False)
    records = []
    for row in source.iter_rows(named=True):
        day = date.fromisoformat(row["date"])
        if day.year != year:
            continue
        events = [datetime.fromisoformat(row[k]) for k in
                  ("sunrise_local", "solar_noon_local", "sunset_local")]
        zone = ZoneInfo(row["timezone"])
        if any(t.tzinfo is None or t.date() != day or t.utcoffset() != t.astimezone(zone).utcoffset()
               for t in events):
            raise ValueError("Solar events must use the stated local date and timezone offset")
        if len({t.utcoffset() for t in events}) != 1 or not events[0] < events[1] < events[2]:
            raise ValueError("This curve adapter requires ordered events without a daylight DST jump")
        hours = [t.hour + t.minute / 60 + t.second / 3600 for t in events]
        records.append(dict(date=day, sunrise_hour=hours[0], solar_noon_hour=hours[1], sunset_hour=hours[2]))
    expected = [date(year, 1, 1) + timedelta(days=i) for i in range(366 if calendar.isleap(year) else 365)]
    if sorted(r["date"] for r in records) != expected:
        raise ValueError("Solar calendar must contain exactly one row per target-year day")
    return pl.DataFrame(records).sort("date")


def hourly_model_energy(starts, daily_kwh, sunrise_hour, solar_noon_hour, sunset_hour,
                        width=0.45, max_power_kw=3.85):
    """Integrate kW over each one-hour interval to return modeled kWh."""
    starts = np.asarray(starts, dtype=float)
    if starts.ndim != 1 or not np.isfinite(starts).all():
        raise ValueError("Interval starts must be a finite one-dimensional array")
    return gaussian_interval_energy(starts, starts+1, daily_kwh, sunrise_hour, solar_noon_hour,
                                     sunset_hour, width, max_power_kw)


def accuracy_metrics(measured, modeled):
    """Interval-energy errors, not a percentage of prediction 'accuracy'."""
    measured, modeled = np.asarray(measured, dtype=float), np.asarray(modeled, dtype=float)
    if measured.shape != modeled.shape or measured.ndim != 1 or measured.size == 0:
        raise ValueError("Measured and modeled arrays must have matching nonempty shapes")
    if not np.isfinite(measured).all() or not np.isfinite(modeled).all() or (measured < 0).any() or (modeled < 0).any():
        raise ValueError("Interval energies must be finite and nonnegative")
    residual = modeled - measured
    total = float(measured.sum())
    sst = float(np.sum((measured - measured.mean()) ** 2))
    return dict(mae_kwh=float(np.mean(np.abs(residual))),
                rmse_kwh=float(np.sqrt(np.mean(residual**2))),
                max_absolute_error_kwh=float(np.max(np.abs(residual))),
                wape_percent=float(100 * np.abs(residual).sum() / total) if total else None,
                r_squared=float(1 - np.sum(residual**2) / sst) if sst else None,
                measured_total_kwh=total, modeled_total_kwh=float(modeled.sum()))


def interval_starts_local(labels, day: date, input_timezone="Europe/Amsterdam", interval_label="start"):
    """Convert explicitly assumed timestamp semantics to solar local clock hours.

    Duration is one elapsed hour. Ambiguous/nonexistent labels and intervals
    crossing an offset transition are rejected rather than guessed.
    """
    if interval_label not in ("start", "end"):
        raise ValueError("interval_label must be start or end")
    source_zone, solar_zone = ZoneInfo(input_timezone), ZoneInfo("Europe/Amsterdam")
    hours = []
    for label in labels:
        if label.tzinfo is not None:
            raise ValueError("This adapter requires naive export labels")
        t = label.replace(tzinfo=source_zone)
        if t.utcoffset() != label.replace(tzinfo=source_zone, fold=1).utcoffset():
            raise ValueError("Ambiguous/nonexistent DST label requires explicit resolution")
        start = t.astimezone(timezone.utc) - (timedelta(hours=1) if interval_label == "end" else timedelta())
        a, b = start.astimezone(solar_zone), (start + timedelta(hours=1)).astimezone(solar_zone)
        if a.utcoffset() != b.utcoffset():
            raise ValueError("Comparison adapter does not support a DST-crossing interval")
        hours.append((a.date() - day).days * 24 + a.hour + a.minute / 60 + a.second / 3600)
    return np.array(hours)


def select_monthly_peak_days(daily: pl.DataFrame) -> pl.DataFrame:
    """Select each month's maximum measured day across years; break ties by date."""
    peaks = (daily.with_columns(pl.col("date").dt.month().alias("month"))
             .sort(["month", "daily_kwh", "date"], descending=[False, True, False])
             .unique(subset="month", keep="first", maintain_order=True))
    if peaks["month"].to_list() != list(range(1, 13)):
        raise ValueError("Monthly comparison requires observations in all twelve months")
    return peaks


def plot_measured_comparison(starts, measured, modeled, events, metrics, width=0.45, max_power_kw=3.85,
                             assumption="Europe/Amsterdam; interval-start labels"):
    """Overlay interval-average measurements and the smooth curve; plot errors."""
    r, p, s = (events[k] for k in ("sunrise_hour", "solar_noon_hour", "sunset_hour"))
    grid = np.linspace(0, 24, 1441)
    curve = gaussian_power(grid, float(np.sum(measured)), r, p, s, width, max_power_kw)
    fig, (ax, residual_ax) = plt.subplots(2, 1, figsize=(13, 7), sharex=True,
                                         gridspec_kw={"height_ratios": [3, 1]})
    ax.bar(starts, measured, width=1, align="edge", color="#929da6", alpha=0.4,
           edgecolor="white", label="Measured hourly-average power")
    ax.stairs(measured, np.r_[starts, starts[-1]+1], color="#35424b", linewidth=1.6)
    ax.plot(grid, curve, color="#8b438b", linewidth=2.2, label="Capped Gaussian power")
    ax.stairs(modeled, np.r_[starts, starts[-1]+1], color="#d56835", linestyle="--",
              linewidth=1.8, label="Modeled hourly-average power")
    ax.axhline(max_power_kw, color="0.5", linestyle=":", label=f"Ceiling: {max_power_kw:g} kW")
    ax.set(ylabel="Production (kW; bars are hourly averages)", ylim=(0, max_power_kw*1.32),
           title=f"Highest-yield July day: {events['date']} · measured total {np.sum(measured):.2f} kWh")
    ax.legend(loc="upper left", fontsize=8)
    r_squared = f"{metrics['r_squared']:.3f}" if metrics['r_squared'] is not None else "undefined"
    wape = f"{metrics['wape_percent']:.1f}%" if metrics['wape_percent'] is not None else "undefined"
    text = (f"Hourly energy error (24 intervals)\nMAE: {metrics['mae_kwh']:.3f} kWh\n"
            f"RMSE: {metrics['rmse_kwh']:.3f} kWh\nWAPE: {wape}\nR²: {r_squared}")
    ax.text(0.99, 0.97, text, transform=ax.transAxes, va="top", ha="right", fontsize=9,
            bbox={"facecolor": "white", "edgecolor": "0.8", "alpha": 0.95})
    residual = modeled-measured
    residual_ax.bar(starts+0.5, residual, width=0.9, color=np.where(residual>=0, "#d56835", "#5484a0"))
    residual_ax.axhline(0, color="0.4", linewidth=0.8)
    residual_ax.set(ylabel="Model − measured\n(kWh per hour)", xlabel="Local hour (Europe/Amsterdam)",
                    xlim=(0, 24), xticks=np.arange(0, 25, 2))
    for axis in (ax, residual_ax):
        axis.grid(axis="y", alpha=0.2)
        axis.set_axisbelow(True)
    fig.text(0.5, 0.01, f"Assumed export: {assumption}. Daily energy is supplied, not predicted; shape settings fixed.",
             ha="center", fontsize=9)
    fig.tight_layout(rect=(0, 0.035, 1, 1))
    return fig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yields", type=Path, required=True)
    parser.add_argument("--solar-calendar", type=Path, required=True)
    parser.add_argument("--input-timezone", default="Europe/Amsterdam")
    parser.add_argument("--interval-label", choices=["start", "end"], default="start")
    parser.add_argument("--width", type=float, default=0.45)
    parser.add_argument("--max-power-kw", type=float, default=3.85)
    parser.add_argument("--all-months", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("outputs/solar_comparison"))
    args = parser.parse_args()
    peaks = select_monthly_peak_days(read_daily_yields(args.yields))
    if not args.all_months:
        peaks = peaks.filter(pl.col("month")==7)
    source = pl.read_csv(args.yields, separator="\t", try_parse_dates=True)
    calendars = pl.concat([read_solar_calendar(args.solar_calendar, year)
                            for year in sorted({d.year for d in peaks["date"].to_list()})])
    examples, tables, records = [], [], []
    for row in peaks.iter_rows(named=True):
        day = row["date"]
        selected = source.filter(pl.col("date_time").dt.date()==day).sort("date_time")
        events = calendars.filter(pl.col("date")==day).row(0,named=True)
        r,p,s = (events[k] for k in ("sunrise_hour","solar_noon_hour","sunset_hour"))
        measured = selected["hourly_yield_kwh"].to_numpy()
        starts = interval_starts_local(selected["date_time"].to_list(), day, args.input_timezone, args.interval_label)
        modeled = hourly_model_energy(starts, row["daily_kwh"], r,p,s,args.width,args.max_power_kw)
        score = accuracy_metrics(measured,modeled)
        mask = (starts<s)&(starts+1>r)
        records.append(dict(date=str(day),month=day.month,metrics=score,
                            daylight_metrics=accuracy_metrics(measured[mask],modeled[mask])))
        tables.append(selected.with_columns(pl.Series("local_interval_start_hour",starts),
                       pl.Series("modeled_hourly_kwh",modeled),pl.Series("model_minus_measured_kwh",modeled-measured)))
        examples.append((starts,measured,modeled,events,score))
    report=dict(source=str(args.yields),solar_calendar=str(args.solar_calendar),
                solar_calendar_attribution="https://sunrise-sunset.org/",width=args.width,max_power_kw=args.max_power_kw,
                input_timezone_assumption=args.input_timezone,interval_label_assumption=args.interval_label,
                selection="Maximum yield per selected month across years; earliest date breaks ties",
                examples=records,limitations="Known daily total supplied, not forecast. Export conventions unconfirmed.")
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/"accuracy.json").write_text(json.dumps(report,indent=2)+"\n")
    pl.concat(tables).write_csv(args.output/"hourly_comparison.txt",separator="\t")
    if not args.all_months:
        fig=plot_measured_comparison(*examples[0],args.width,args.max_power_kw,
                                     f"{args.input_timezone}; interval-{args.interval_label} labels")
        fig.savefig(args.output/"july_measured_vs_modeled.png",dpi=180)
    else:
        fig,axes=plt.subplots(4,3,figsize=(17,15),sharex=True,sharey=True)
        hours=np.linspace(0,24,721)
        for ax,(starts,measured,modeled,events,score) in zip(axes.flat,examples):
            ax.bar(starts,measured,width=1,align="edge",color="#b0b7bd",alpha=0.6,label="Measured hourly average")
            curve=gaussian_power(hours,float(measured.sum()),events["sunrise_hour"],events["solar_noon_hour"],events["sunset_hour"],args.width,args.max_power_kw)
            ax.plot(hours,curve,color="#8b438b",label="Capped Gaussian")
            ax.set_title(f"{calendar.month_name[events['date'].month]} · {events['date']} · {measured.sum():.2f} kWh",fontsize=10)
            ax.text(0.025,0.97,f"RMSE: {score['rmse_kwh']:.3f} kWh",transform=ax.transAxes,va="top",fontsize=8)
            ax.set(xlim=(0,24),ylim=(0,args.max_power_kw*1.25),xticks=[0,6,12,18,24])
            ax.grid(axis="y",alpha=0.2)
        handles,labels=axes.flat[0].get_legend_handles_labels()
        fig.legend(handles,labels,loc="upper center",ncol=2)
        fig.supxlabel("Local hour (Europe/Amsterdam)")
        fig.supylabel("Production (kW; bars are hourly averages)")
        fig.tight_layout(rect=(0.02,0.02,1,0.97))
        fig.savefig(args.output/"monthly_measured_vs_modeled.png",dpi=160)
    plt.close("all")
    print(pl.DataFrame([dict(date=r["date"], **r["metrics"]) for r in records]))
    print(f"Saved comparison data, accuracy metrics and plots to {args.output}")


if __name__ == "__main__":
    main()
