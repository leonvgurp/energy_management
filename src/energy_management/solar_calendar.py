"""Cached locality lookup and daily solar events; no household measurements."""

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import polars as pl


def _cached_request(base_url, params, cache_dir, offline):
    url = base_url + "?" + urlencode(params)
    path = Path(cache_dir) / (hashlib.sha256(url.encode()).hexdigest()[:20] + ".json")
    if path.exists():
        return json.loads(path.read_text())
    if offline:
        raise ValueError(f"No cached response for {url}; run once without --offline")
    request = Request(url, headers={"User-Agent": "energy-management/0.1"})
    with urlopen(request, timeout=20) as response:
        data = json.load(response)
    if "error" in data:
        raise ValueError(f"API rejected request: {data}")
    record = dict(url=url, retrieved_at=datetime.now(timezone.utc).isoformat(), data=data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def solar_calendar(config, year, cache_dir="data/solar_cache", offline=False):
    """Return validated local events and provenance, fetching only cache misses."""
    geocoding = None
    if config.latitude is None:
        params = dict(name=config.locality, count=10)
        if config.country_code:
            params["countryCode"] = config.country_code
        geocoding = _cached_request("https://geocoding-api.open-meteo.com/v1/search", params, cache_dir, offline)
        results = geocoding["data"].get("results", [])
        exact = [r for r in results if r["name"].casefold() == config.locality.casefold()]
        matches = exact or results
        if len(matches) != 1:
            raise ValueError("Locality is missing or ambiguous; add country_code or explicit latitude/longitude")
        location = {k: matches[0][k] for k in ("name", "latitude", "longitude", "timezone", "country_code")}
    else:
        location = dict(name=config.locality, latitude=config.latitude, longitude=config.longitude)
    params = dict(lat=location["latitude"], lng=location["longitude"],
                  date_start=f"{year}-01-01", date_end=f"{year}-12-31")
    if "timezone" in location:
        params["tz"] = location["timezone"]
    response = _cached_request("https://api.sunrise-sunset.org/v2", params, cache_dir, offline)
    data = response["data"]
    zone = ZoneInfo(data["tzid"])
    if any(abs(data[k]-location[name]) > 1e-4 for k, name in (("lat", "latitude"), ("lng", "longitude"))):
        raise ValueError("Solar response coordinates do not match requested locality")
    location["timezone"] = data["tzid"]
    rows = []
    for day in data["days"]:
        events = [datetime.fromisoformat(day[key]) if day.get(key) else None
                  for key in ("sunrise", "solar_noon", "sunset")]
        if any(t is None for t in events):
            raise ValueError("This daylight model requires sunrise and sunset every day; polar days are unsupported")
        if not events[0] < events[1] < events[2]:
            raise ValueError("Invalid solar event ordering")
        if any(t.tzinfo is None or t.date().isoformat() != day["date"] or
               t.utcoffset() != t.astimezone(zone).utcoffset() for t in events):
            raise ValueError("Solar events have inconsistent dates/timezone offsets")
        if len({t.utcoffset() for t in events}) != 1:
            raise ValueError("DST changes within daylight are unsupported")
        row = dict(date=date.fromisoformat(day["date"]))
        row.update({key: t.hour+t.minute/60+t.second/3600
                    for key, t in zip(("sunrise_hour", "solar_noon_hour", "sunset_hour"), events)})
        rows.append(row)
    expected = [date(year, 1, 1)+timedelta(days=i) for i in range((date(year+1, 1, 1)-date(year, 1, 1)).days)]
    if sorted(r["date"] for r in rows) != expected:
        raise ValueError("Solar calendar must have exactly one record for every requested date")
    provenance = dict(location=location, solar_source={k: v for k, v in response.items() if k != "data"},
                      attribution="https://sunrise-sunset.org/")
    if geocoding:
        provenance["geocoding_source"] = {k: v for k, v in geocoding.items() if k != "data"}
        provenance["geocoding_attribution"] = "https://open-meteo.com/en/docs/geocoding-api"
    return pl.DataFrame(rows).sort("date"), provenance
