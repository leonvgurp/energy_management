"""Shared elapsed-hour calendar and approximate solar geometry."""
import calendar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import numpy as np


def calendar_hours(year, zone_name):
    zone = ZoneInfo(zone_name)
    start = datetime(year, 1, 1, tzinfo=zone).astimezone(timezone.utc)
    stop = datetime(year+1, 1, 1, tzinfo=zone).astimezone(timezone.utc)
    utc = [start+timedelta(hours=i) for i in range(int((stop-start).total_seconds()/3600))]
    return utc, [t.astimezone(zone) for t in utc]


def darkness(utc, latitude, longitude):
    """Approximate solar elevation at interval midpoint (NOAA fractional-year formula).

    Binary sun-below-horizon proxy; not measured illumination, shading or clouds.
    """
    midpoint = [t+timedelta(minutes=30) for t in utc]
    day = np.array([t.timetuple().tm_yday for t in midpoint])
    hour = np.array([t.hour+t.minute/60 for t in midpoint])
    year_lengths = np.array([366 if calendar.isleap(t.year) else 365 for t in midpoint])
    gamma = 2*np.pi/year_lengths*(day-1+(hour-12)/24)
    eqtime = 229.18*(.000075+.001868*np.cos(gamma)-.032077*np.sin(gamma)
                     -.014615*np.cos(2*gamma)-.040849*np.sin(2*gamma))
    decl = (.006918-.399912*np.cos(gamma)+.070257*np.sin(gamma)
            -.006758*np.cos(2*gamma)+.000907*np.sin(2*gamma)
            -.002697*np.cos(3*gamma)+.00148*np.sin(3*gamma))
    angle = np.deg2rad((hour*60+eqtime+4*longitude)/4-180)
    lat = np.deg2rad(latitude)
    sine_elevation = np.sin(lat)*np.sin(decl)+np.cos(lat)*np.cos(decl)*np.cos(angle)
    return (sine_elevation < 0).astype(float)


