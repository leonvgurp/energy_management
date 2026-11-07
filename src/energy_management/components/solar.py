"""Solar scenarios from summary statistics or empirical historical distributions."""

from __future__ import annotations

import argparse
import calendar
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import json
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo

import matplotlib.pyplot as plt
import numpy as np
import polars as pl
from scipy.integrate import quad
from scipy.optimize import brentq
from scipy.special import erfc
from ..energy_tables import write_energy_tables
from scipy.stats import beta
import seaborn as sns

from ..solar_calendar import solar_calendar


@dataclass(frozen=True)
class SolarConfig:
    locality: str
    monthly_kwh: tuple[float, ...]
    max_daily_kwh: tuple[float, ...]
    max_power_kw: float
    country_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    beta_concentration: float = 2.0
    width: float = 0.45

    def __post_init__(self):
        if not isinstance(self.locality, str) or not self.locality.strip():
            raise ValueError("A locality name is required")
        for name in ("monthly_kwh", "max_daily_kwh"):
            values = np.asarray(getattr(self, name), dtype=float)
            if values.shape != (12,) or not np.isfinite(values).all() or (values < 0).any():
                raise ValueError(f"{name} must contain twelve finite nonnegative values, January first")
            object.__setattr__(self, name, tuple(values.tolist()))
        if not np.isfinite(self.max_power_kw) or self.max_power_kw <= 0:
            raise ValueError("max_power_kw must be finite and positive")
        if not np.isfinite(self.beta_concentration) or self.beta_concentration <= 0:
            raise ValueError("beta_concentration must be finite and positive")
        if not np.isfinite(self.width) or not 0.05 <= self.width <= 2:
            raise ValueError("width must be between 0.05 and 2")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Provide both latitude and longitude, or neither")
        if self.latitude is not None and not (np.isfinite([self.latitude, self.longitude]).all()
                and -90 <= self.latitude <= 90 and -180 <= self.longitude <= 180):
            raise ValueError("Invalid coordinates")

    @classmethod
    def load(cls, path):
        with Path(path).open("rb") as stream:
            return cls(**tomllib.load(stream))


def _bounded_scale(weights, total, caps):
    """Solve sum(min(day_cap, scale * weight)) = monthly energy."""
    caps = np.asarray(caps, dtype=float)
    if not np.isfinite(total) or total < 0 or total > caps.sum():
        raise ValueError("Monthly energy cannot fit under daily maxima and daylight power capacity")
    if total == 0:
        return np.zeros(len(caps))
    if total == caps.sum():
        return caps.copy()
    weights = np.maximum(np.asarray(weights, dtype=float), np.finfo(float).eps)
    scale = brentq(lambda a: np.minimum(caps, a*weights).sum()-total,
                    0, float(np.max(caps/weights)), xtol=1e-12)
    result = np.minimum(caps, scale*weights)
    if not np.isclose(result.sum(), total, rtol=0, atol=1e-8):
        raise ValueError("Could not conserve monthly energy")
    return result


def generate_year(config: SolarConfig, events: pl.DataFrame, seed: int = 42,
                  historical_daily: pl.DataFrame | None = None,
                  daily_weather: pl.DataFrame | None = None) -> pl.DataFrame:
    """Stratified Beta or empirical quantiles with bounded exact monthly totals.

    Means and caps do not identify weather variability. Beta concentration is
    an explicit assumption; no historical hourly export is needed.
    """
    if daily_weather is not None and historical_daily is not None:
        raise ValueError("Choose historical distributions or shared daily weather")
    if daily_weather is not None:
        daily_weather = daily_weather.sort("date")
        if daily_weather["date"].to_list() != events.sort("date")["date"].to_list():
            raise ValueError("Daily weather must cover the solar calendar exactly")
        weights = daily_weather["radiation_kwh_m2"].to_numpy()
        if not np.isfinite(weights).all() or (weights < 0).any():
            raise ValueError("Daily radiation must be finite and nonnegative")
    events = events.sort("date")
    if events.is_empty():
        raise ValueError("An entire solar calendar year is required")
    year = events["date"][0].year
    start = date(year, 1, 1)
    expected = [start+timedelta(days=i) for i in range((date(year+1, 1, 1)-start).days)]
    if events["date"].to_list() != expected:
        raise ValueError("Solar calendar must contain each target-year date exactly once")
    hours = events.select("sunrise_hour", "solar_noon_hour", "sunset_hour").to_numpy()
    if not np.isfinite(hours).all() or not ((0 <= hours[:, 0]) & (hours[:, 0] < hours[:, 1]) &
                                           (hours[:, 1] < hours[:, 2]) & (hours[:, 2] <= 24)).all():
        raise ValueError("Solar event hours must be finite and ordered")
    rows = []
    for m, child in zip(range(1, 13), np.random.SeedSequence(seed).spawn(12)):
        month = events.filter(pl.col("date").dt.month() == m)
        total, maximum, n = config.monthly_kwh[m-1], config.max_daily_kwh[m-1], month.height
        # Strictly below power * daylight: continuous zero-endpoint curves
        # cannot deliver a full rectangle. The tiny margin is numerical only.
        caps = np.minimum(maximum, config.max_power_kw *
                          (month["sunset_hour"]-month["sunrise_hour"]).to_numpy() * (1-1e-8))
        if total > caps.sum():
            raise ValueError(f"{calendar.month_name[m]} target {total:g} kWh is infeasible under its caps")
        rng = np.random.default_rng(child)
        if total == 0:
            daily = np.zeros(n)
        else:
            mean_fraction = total/(n*maximum)
            q = (np.arange(n)+0.5+rng.uniform(-0.2, 0.2, n))/n
            if historical_daily is not None:
                samples = historical_daily.filter(pl.col("date").dt.month() == m)["daily_kwh"].to_numpy()
                if not len(samples) or not np.isfinite(samples).all() or (samples < 0).any():
                    raise ValueError("Historical daily samples must be finite, nonnegative and cover all months")
                weights = np.quantile(samples, q)
            else:
                weights = (np.ones(n) if mean_fraction >= 1 else
                           beta.ppf(q, mean_fraction*config.beta_concentration,
                                    (1-mean_fraction)*config.beta_concentration))
            if daily_weather is None:
                rng.shuffle(weights)
            else:
                weights = daily_weather.filter(pl.col("date").dt.month() == m)["radiation_kwh_m2"].to_numpy()
                if not np.any(weights > 0):
                    raise ValueError("Positive solar target requires positive monthly radiation")
            daily = _bounded_scale(weights, total, caps)
        rows.extend(dict(date=d, daily_kwh=float(e)) for d, e in zip(month["date"].to_list(), daily))
    return pl.DataFrame(rows)


@lru_cache(maxsize=4096)
def _power_amplitude(daily_kwh: float, daylight_hours: float, width: float,
                     max_power_kw: float | None) -> float:
    """Solve the capped integral, using the plateau's normalized half-width.

    Both sides have the same normalized shape, so their durations add. Solving
    for the plateau boundary in [0, 1] avoids an unbounded amplitude search.
    """
    b = np.exp(-0.5 / width**2)

    def shape(u):
        # expm1 avoids cancellation at the sunrise/sunset boundary.
        return b * np.expm1((1 - u**2) / (2 * width**2)) / (1 - b)

    def tail_area(u):
        if u > 0.99:
            # Stable near the full-day capacity limit, where erf differences cancel.
            return quad(shape, u, 1, epsabs=1e-15, epsrel=1e-10)[0]
        return (width * np.sqrt(np.pi / 2) *
                (erfc(u / (np.sqrt(2) * width)) - erfc(1 / (np.sqrt(2) * width)))
                - b * (1 - u)) / (1 - b)

    uncapped = daily_kwh / (daylight_hours * tail_area(0))
    if max_power_kw is None:
        return uncapped
    if not np.isfinite(max_power_kw) or max_power_kw <= 0:
        raise ValueError("max_power_kw must be finite and positive, or None")
    capacity = max_power_kw * daylight_hours
    if daily_kwh >= capacity:
        raise ValueError(
            f"Daily energy {daily_kwh:g} kWh is infeasible: it must be below "
            f"{capacity:g} kWh at {max_power_kw:g} kW within daylight"
        )
    if uncapped <= max_power_kw:
        return uncapped

    def energy(u):
        if u == 1:
            return capacity  # limiting rectangular curve, not a finite amplitude
        return capacity * (u + tail_area(u) / shape(u))

    boundary = brentq(lambda u: energy(u) - daily_kwh, 0, 1, xtol=1e-14)
    return max_power_kw / shape(boundary)


def gaussian_power(hours, daily_kwh: float, sunrise_hour: float, solar_noon_hour: float,
                   sunset_hour: float, width: float = 0.45,
                   max_power_kw: float | None = 3.85) -> np.ndarray:
    """Daylight-bounded, two-sided tapered Gaussian power in kW.

    Input hours are local decimal clock hours. The two sides use their own
    sunrise-to-noon/noon-to-sunset duration. A power ceiling produces a plateau
    containing solar noon. Amplitude preserves daily_kwh exactly.
    Set max_power_kw=None for the uncapped curve. This adapter suits Leusden,
    where DST clock changes happen outside daylight hours.
    """
    if not (np.isfinite([daily_kwh, sunrise_hour, solar_noon_hour, sunset_hour, width]).all()
            and daily_kwh >= 0 and 0 <= sunrise_hour < solar_noon_hour < sunset_hour <= 24
            and 0.05 <= width <= 2):
        raise ValueError("Invalid energy, daylight boundaries, or width (0.05–2)")
    peak_hour = solar_noon_hour
    hours = np.asarray(hours, dtype=float)
    if not np.isfinite(hours).all():
        raise ValueError("Hours must be finite")
    left, right = peak_hour - sunrise_hour, sunset_hour - peak_hour
    u = np.abs(hours - peak_hour) / np.where(hours <= peak_hour, left, right)
    b = np.exp(-0.5 / width**2)
    bounded_u = np.minimum(u, 1)
    shape = b * np.expm1((1 - bounded_u**2) / (2 * width**2)) / (1 - b)
    amplitude = _power_amplitude(daily_kwh, left + right, width, max_power_kw)
    power = amplitude * shape
    return power if max_power_kw is None else np.minimum(max_power_kw, power)


def gaussian_interval_energy(starts, ends, daily_kwh, sunrise_hour, solar_noon_hour,
                             sunset_hour, width=0.45, max_power_kw=3.85):
    """Analytical integrals of capped power over arbitrary clock-hour intervals."""
    starts, ends = np.broadcast_arrays(np.asarray(starts, dtype=float), np.asarray(ends, dtype=float))
    if not np.isfinite(starts).all() or not np.isfinite(ends).all() or (ends < starts).any():
        raise ValueError("Intervals must have finite, ordered start/end times")
    peak = solar_noon_hour
    gaussian_power([peak], daily_kwh, sunrise_hour, solar_noon_hour, sunset_hour,
                   width, max_power_kw)
    left, right = peak - sunrise_hour, sunset_hour - peak
    amplitude = _power_amplitude(daily_kwh, left + right, width, max_power_kw)
    if amplitude == 0:
        return np.zeros_like(starts)
    b = np.exp(-0.5 / width**2)
    clipped = max_power_kw is not None and amplitude > max_power_kw
    edge = np.sqrt(-2 * width**2 * np.log(b + (1-b)*max_power_kw/amplitude)) if clipped else 0.0

    def half_integral(u):
        v = np.maximum(u, edge)
        tail = (width * np.sqrt(np.pi/2) *
                (erfc(edge/(np.sqrt(2)*width)) - erfc(v/(np.sqrt(2)*width)))
                - b*(v-edge)) / (1-b)
        return (max_power_kw*np.minimum(u, edge) if clipped else 0.0) + amplitude*tail

    full_half = half_integral(1.0)

    def primitive(t):
        t = np.clip(t, sunrise_hour, sunset_hour)
        u_left = np.clip((peak-t)/left, 0, 1)
        u_right = np.clip((t-peak)/right, 0, 1)
        return np.where(t <= peak, left*(full_half-half_integral(u_left)),
                        left*full_half + right*half_integral(u_right))

    energy = np.maximum(0.0, primitive(ends)-primitive(starts))
    return energy if max_power_kw is None else np.minimum(energy, max_power_kw*(ends-starts))


def plot_yield_heatmap(daily: pl.DataFrame, max_daily_kwh: float):
    """Months in columns, days in rows; empty calendar cells stay blank."""
    grid = np.full((31, 12), np.nan)
    for day, value in daily.select("date", "daily_kwh").iter_rows():
        grid[day.day - 1, day.month - 1] = value
    fig, ax = plt.subplots(figsize=(14, 14))
    sns.heatmap(grid, mask=np.isnan(grid), ax=ax, cmap="magma", vmin=0,
                vmax=max_daily_kwh, annot=True, fmt=".1f", annot_kws={"fontsize": 8},
                linewidths=0.4, xticklabels=list(calendar.month_abbr)[1:],
                yticklabels=np.arange(1, 32), cbar_kws={"label": "Modeled daily energy (kWh)"})
    ax.set(xlabel="Month", ylabel="Day of month", title="Modeled daily solar yield · fixed monthly totals")
    ax.tick_params(axis="y", rotation=0)
    fig.tight_layout()
    return fig


def plot_example_curves(daily: pl.DataFrame, solar_calendar: pl.DataFrame, width: float = 0.45,
                        max_power_kw: float | None = 3.85):
    """Lowest/highest modeled days in January and July; common power scale."""
    combined = daily.join(solar_calendar, on="date", validate="1:1")
    if combined.height != daily.height:
        raise ValueError("Missing solar dates")
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8), sharey=True)
    hours = np.linspace(0, 24, 1441)
    for month, ax in zip((1, 7), axes):
        selected = combined.filter(pl.col("date").dt.month() == month).sort("daily_kwh", "date")
        for row, label, color in ((selected.row(0, named=True), "Low", "#6b4283"),
                                  (selected.row(-1, named=True), "High", "#ed7953")):
            args = (row["daily_kwh"], row["sunrise_hour"], row["solar_noon_hour"],
                    row["sunset_hour"], width, max_power_kw)
            power = gaussian_power(hours, *args)
            ax.plot(hours, power, color=color, linewidth=2,
                    label=f'{label}: {row["date"]} · {row["daily_kwh"]:.2f} kWh')
            ax.fill_between(hours, power, color=color, alpha=0.10)
            for event, marker in (("sunrise_hour", "^"), ("solar_noon_hour", "o"), ("sunset_hour", "v")):
                value = gaussian_power([row[event]], *args)[0]
                ax.scatter(row[event], value, marker=marker, color=color, s=30, zorder=3)
        if max_power_kw is not None:
            ax.axhline(max_power_kw, color="0.4", linestyle="--", linewidth=1,
                       label=f"Power ceiling: {max_power_kw:g} kW")
        ax.set(title=calendar.month_name[month], xlabel="Local hour (Europe/Amsterdam)",
               xlim=(0, 24), xticks=np.arange(0, 25, 3))
        ax.legend(fontsize=8, loc="upper left")
        ax.grid(alpha=0.2)
    axes[0].set_ylabel("Modeled solar production (kW)")
    fig.suptitle("Power-capped Gaussian curves · ▲ sunrise  ● solar noon  ▼ sunset")
    fig.tight_layout()
    return fig


def hourly_yields(daily, events, config, tzid):
    """One elapsed-hour interval per row, with UTC and offset-aware local labels."""
    zone = ZoneInfo(tzid)
    rows = []
    for row in daily.join(events, on="date", validate="1:1").iter_rows(named=True):
        day = row["date"]
        next_day = day+timedelta(days=1)
        first = datetime(day.year, day.month, day.day, tzinfo=zone).astimezone(timezone.utc)
        last = datetime(next_day.year, next_day.month, next_day.day, tzinfo=zone).astimezone(timezone.utc)
        seconds = (last-first).total_seconds()
        if seconds % 3600:
            raise ValueError("Hourly output does not support non-hour timezone transitions")
        utc = [first+timedelta(hours=i) for i in range(int(seconds/3600)+1)]
        local = [t.astimezone(zone) for t in utc]
        hours = np.array([(t.date()-day).days*24+t.hour+t.minute/60 for t in local])
        energies = gaussian_interval_energy(hours[:-1], hours[1:], row["daily_kwh"],
                    row["sunrise_hour"], row["solar_noon_hour"], row["sunset_hour"], config.width, config.max_power_kw)
        if not np.isclose(energies.sum(), row["daily_kwh"], rtol=0, atol=1e-7):
            raise ValueError(f"Hourly energy does not match the daily target on {day}")
        rows.extend(dict(interval_start_utc=u.isoformat(), interval_start_local=t.isoformat(), yield_kwh=float(e))
                    for u, t, e in zip(utc, local, energies))
    return pl.DataFrame(rows)


def monthly_summary(config, daily):
    rows = []
    for m in range(1, 13):
        values = daily.filter(pl.col("date").dt.month() == m)["daily_kwh"]
        rows.append(dict(month=m, target_kwh=config.monthly_kwh[m-1], generated_kwh=values.sum(),
                         daily_upper_bound_kwh=config.max_daily_kwh[m-1], generated_max_daily_kwh=values.max()))
    return pl.DataFrame(rows)


def plot_saved_outputs(output: Path):
    """Render plots using only the saved, common model outputs."""
    daily = pl.read_csv(output / "daily_yields.txt", separator="\t", try_parse_dates=True)
    parameters = pl.read_csv(output / "daily_curve_parameters.txt", separator="\t", try_parse_dates=True)
    summary = pl.read_csv(output / "monthly_summary.txt", separator="\t")
    width, cap = parameters.select("gaussian_width_fraction", "max_power_kw").row(0)
    plot_yield_heatmap(daily, max(summary["daily_upper_bound_kwh"].max(), 1.0)).savefig(
        output / "daily_heatmap.png", dpi=160)
    curves = plot_example_curves(daily, parameters.drop("daily_kwh"), width, cap)
    curves.text(0.5, 0.01, "Solar calendar: https://sunrise-sunset.org/ · smooth modeled production", ha="center", fontsize=8)
    curves.tight_layout(rect=(0, 0.04, 1, 1))
    curves.savefig(output / "january_july_curves.png", dpi=180)
    plt.close("all")


def main(method="statistics"):
    parser = argparse.ArgumentParser(description=f"Generate solar scenarios from {method}")
    if method == "statistics":
        parser.add_argument("--config", type=Path, required=True)
    else:
        parser.add_argument("--yields", type=Path, required=True)
        parser.add_argument("--locality", required=True)
        parser.add_argument("--country-code")
        parser.add_argument("--max-power-kw", type=float,
                            help="Inverter ceiling; defaults to the measured maximum hourly-average power")
        parser.add_argument("--input-timezone", required=True)
        parser.add_argument("--interval-label", required=True, choices=["start"],
                            help="This adapter accepts naive hourly interval-start labels only")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/solar_cache"))
    parser.add_argument("--offline", action="store_true", help="Use cached locality and solar responses only")
    parser.add_argument("--output", type=Path, default=Path("outputs/solar"))
    args = parser.parse_args()
    historical = None
    if method == "statistics":
        config = SolarConfig.load(args.config)
        source = dict(config_path=str(args.config))
    else:
        from ..solar_history import fit_history
        config, historical, source = fit_history(args.yields, args.locality, args.country_code,
                                                 args.max_power_kw, args.input_timezone)
    events, provenance = solar_calendar(config, args.year, args.cache_dir, args.offline)
    tzid = provenance["location"]["timezone"]
    if method == "history" and args.input_timezone != tzid:
        raise ValueError("Input timezone must match the locality timezone for this adapter")
    daily = generate_year(config, events, args.seed, historical)
    hourly = hourly_yields(daily, events, config, tzid)
    summary = monthly_summary(config, daily)
    args.output.mkdir(parents=True, exist_ok=True)
    write_energy_tables(hourly, args.output, 'yield_kwh', tzid)
    for name, frame in (("daily_yields", daily), ("hourly_yields", hourly),
                        ("monthly_summary", summary), ("solar_calendar", events)):
        frame.write_csv(args.output / f"{name}.txt", separator="\t")
    daily.join(events, on="date").with_columns(pl.lit(config.width).alias("gaussian_width_fraction"),
        pl.lit(config.max_power_kw).alias("max_power_kw")).write_csv(args.output / "daily_curve_parameters.txt", separator="\t")
    metadata = dict(schema_version=1, method=method, config=asdict(config), source=source,
                    year=args.year, seed=args.seed, annual_kwh=sum(config.monthly_kwh), provenance=provenance,
                    assumptions="Empirical monthly quantiles" if historical is not None else "Assumed Beta daily distribution")
    metadata["limitations"] = "Gaussian width assumed. Fixed totals exclude year-to-year uncertainty; not a weather forecast."
    (args.output / "run.json").write_text(json.dumps(metadata, indent=2)+"\n")
    plot_saved_outputs(args.output)
    print(summary)
    print(f"Annual energy: {sum(config.monthly_kwh):.2f} kWh. Saved data and plots to {args.output}")


if __name__ == "__main__":
    main()
