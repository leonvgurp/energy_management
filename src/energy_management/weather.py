"""Shared annual weather assumptions and optional validated hourly input."""
import calendar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
from scipy.signal import lfilter

from .calendar import calendar_hours, darkness
from .heat_pump_weather import validate_weather


def solar_events(year, timezone_name, latitude, longitude):
    """Estimate daylight at 5-minute resolution using the shared elevation proxy.

    Solar noon is approximated by the daylight midpoint. No network lookup.
    Only locations with a sunrise and sunset on every day are supported.
    """
    zone = ZoneInfo(timezone_name)
    first = date(year, 1, 1)
    rows = []
    for i in range((date(year+1, 1, 1)-first).days):
        day = first+timedelta(days=i)
        # Sample in UTC, avoiding imaginary and repeated local clock times.
        start = datetime.combine(day, datetime.min.time(), zone).astimezone(timezone.utc)
        end = datetime.combine(day+timedelta(days=1), datetime.min.time(), zone).astimezone(timezone.utc)
        stamps = [start+timedelta(minutes=5*j) for j in range(int((end-start).total_seconds()/300))]
        # darkness evaluates midpoint +30min; compensate to sample bin centers.
        sun = darkness([t-timedelta(minutes=27.5) for t in stamps], latitude, longitude) == 0
        lit = np.flatnonzero(sun)
        if not len(lit) or lit[0] == 0 or lit[-1] == len(stamps)-1:
            raise ValueError('Require sunrise and sunset within every local day')
        sunrise = stamps[lit[0]].astimezone(zone)
        sunset = (stamps[lit[-1]]+timedelta(minutes=5)).astimezone(zone)
        if sunrise.utcoffset() != sunset.utcoffset():
            raise ValueError('DST changes during daylight are unsupported')
        a = sunrise.hour+sunrise.minute/60
        b = sunset.hour+sunset.minute/60
        rows.append(dict(date=day, sunrise_hour=a, solar_noon_hour=(a+b)/2, sunset_hour=b))
    return pl.DataFrame(rows)


def generate(config, year, events, config_dir=Path('.')):
    """Return one weather frame used by both solar and space heating.

    Monthly statistics mode is an illustrative sensitivity model, not a weather
    forecast or calibrated uncertainty model. File mode accepts the existing
    KNMI/bootstrap weather.txt schema for the selected reference calendar.
    """
    c, zone_name = config['weather'], config['scenario']['timezone']
    utc, local = calendar_hours(year, zone_name)
    if c['mode'] == 'file':
        path = (Path(config_dir)/c['file']).resolve()
        frame = pl.read_csv(path, separator='\t')
        validate_weather(frame, utc[0], utc[-1]+timedelta(hours=1))
        frame = frame.with_columns(pl.Series('interval_start_local', [t.isoformat() for t in local]))
    elif c['mode'] == 'monthly_statistics':
        month = np.array([t.month-1 for t in local])
        days = np.array([t.date() for t in local])
        unique_days, inverse = np.unique(days, return_inverse=True)
        rng = np.random.default_rng(config['scenario']['seed'])
        rho = c['daily_correlation']
        # Stationary AR(1), shared cloud and temperature realization.
        def ar_noise(n, sigma, correlation):
            innovations = rng.normal(0, sigma*np.sqrt(1-correlation**2), n)
            innovations[0] = rng.normal(0, sigma)
            return lfilter([1], [1, -correlation], innovations)
        clouds = np.exp(ar_noise(len(unique_days), c['cloud_log_sigma'], rho)-c['cloud_log_sigma']**2/2)
        cloud_hourly = clouds[inverse]
        noise = ar_noise(len(utc), c['temperature_sigma_c'], c['temperature_hourly_correlation'])
        # Interpolate monthly means through mid-month to avoid abrupt boundaries.
        phase = np.array([t.month-1+(t.day-1+t.hour/24)/calendar.monthrange(year,t.month)[1] for t in local])
        temp = np.interp(phase, np.arange(12)+.5, c['monthly_temperature_c'], period=12)
        temp += noise+c['diurnal_temperature_amplitude_c']*np.cos(2*np.pi*(np.array([t.hour+.5 for t in local])-15)/24)
        event_map = {r['date']: r for r in events.iter_rows(named=True)}
        shape = []
        for t in local:
            e = event_map[t.date()]
            f = (t.hour+.5-e['sunrise_hour'])/(e['sunset_hour']-e['sunrise_hour'])
            shape.append(max(0., np.sin(np.pi*f)) if 0 < f < 1 else 0.)
        radiation = np.array(shape)*cloud_hourly
        # Conserve configured monthly irradiation before the sunshine adjustment.
        for m, target in enumerate(c['monthly_radiation_kwh_m2']):
            mask = month == m
            radiation[mask] *= target/radiation[mask].sum()
        frame = pl.DataFrame(dict(interval_start_utc=[t.isoformat() for t in utc],
            interval_start_local=[t.isoformat() for t in local], outdoor_c=temp,
            humidity_pct=np.full(len(utc), c['humidity_pct']),
            wind_m_s=np.full(len(utc), c['wind_m_s']), radiation_kwh_m2=radiation))
    else:
        raise ValueError('weather.mode must be monthly_statistics or file')
    monthly = np.array([t.month-1 for t in local])
    winter = np.isin(monthly, np.array(c['winter_months'])-1)
    frame = frame.with_columns(
        (pl.col('outdoor_c')+c['temperature_offset_c']+pl.Series(winter*c['winter_temperature_offset_c'])).alias('outdoor_c'),
        (pl.col('radiation_kwh_m2')*c['sunshine_factor']*pl.Series(np.asarray(c['monthly_sunshine_factors'])[monthly])).alias('radiation_kwh_m2'))
    validate_weather(frame, utc[0], utc[-1]+timedelta(hours=1))
    return frame
