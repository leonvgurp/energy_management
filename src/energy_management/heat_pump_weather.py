"""KNMI station observations, normalized to elapsed-hour UTC intervals."""
import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

UTC = timezone.utc


def bounds(config):
    w = config['weather']
    zone = ZoneInfo(w['timezone'])
    years = w['years']
    if not years or any(not isinstance(y, int) for y in years) or years != list(range(min(years), max(years)+1)):
        raise ValueError('weather.years must be ordered contiguous calendar years')
    start = date.fromisoformat(w['start']) if w.get('start') else date(min(years), 1, 1)
    end = date.fromisoformat(w['end']) if w.get('end') else date(max(years) + 1, 1, 1)
    if end <= start:
        raise ValueError('weather.end must follow start (exclusive local date)')
    return (datetime.combine(d, datetime.min.time(), zone).astimezone(UTC) for d in (start, end))


def parse_knmi(raw, station):
    """HH=1..24 ends the hour; T/U are endpoint samples, FH/Q preceding hour."""
    header = None
    rows = []
    for line in raw.splitlines():
        stripped = line.lstrip('# ').strip()
        if stripped.startswith('STN,') or stripped.startswith('STN '):
            header = [v.strip() for v in stripped.split(',')]
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if header is None:
            raise ValueError('KNMI response has no STN column header')
        values = [v.strip() for v in line.split(',')]
        if len(values) != len(header):
            raise ValueError('Malformed KNMI row')
        record = dict(zip(header, values))
        try:
            if int(record['STN']) != station:
                raise ValueError('Unexpected KNMI station')
            hour = int(record['HH'])
            if not 1 <= hour <= 24:
                raise ValueError('Invalid KNMI hour')
            end = datetime.strptime(record['YYYYMMDD'], '%Y%m%d').replace(tzinfo=UTC) + timedelta(hours=hour)
            t, rh, wind, radiation = (float(record[k]) for k in ('T', 'U', 'FH', 'Q'))
        except (KeyError, ValueError) as exc:
            raise ValueError('Missing/invalid required KNMI T/U/FH/Q reading; select another date range or station') from exc
        if not all(np.isfinite(v) for v in (t, rh, wind, radiation)) or not 0 <= rh <= 100 or min(wind, radiation) < 0:
            raise ValueError('Out-of-range KNMI readings')
        rows.append(dict(interval_start_utc=(end-timedelta(hours=1)).isoformat(),
                         outdoor_c=t/10, humidity_pct=rh, wind_m_s=wind/10,
                         radiation_kwh_m2=radiation/360))
    if not rows:
        raise ValueError('No KNMI observations returned')
    return pl.DataFrame(rows).sort('interval_start_utc')


def validate_weather(frame, start, end):
    expected = int((end-start).total_seconds()/3600)
    stamps = [datetime.fromisoformat(x) for x in frame['interval_start_utc']]
    if any(t.tzinfo is None or t.utcoffset() != timedelta(0) for t in stamps):
        raise ValueError('Weather requires UTC offset-aware interval labels')
    if len(stamps) != expected or stamps != [start+timedelta(hours=i) for i in range(expected)]:
        raise ValueError('Missing, duplicate or unordered weather hours; retrieve complete requested coverage')
    for key in ('outdoor_c', 'humidity_pct', 'wind_m_s', 'radiation_kwh_m2'):
        values = frame[key].to_numpy()
        if not np.isfinite(values).all():
            raise ValueError(f'Missing/nonfinite weather {key}')
    if not ((frame['humidity_pct'] >= 0) & (frame['humidity_pct'] <= 100)).all() or frame['wind_m_s'].min() < 0 or frame['radiation_kwh_m2'].min() < 0:
        raise ValueError('Out-of-range weather')
    return frame


def download_weather(config, output, cache_dir, offline=False):
    start, end = bounds(config)
    w = config['weather']
    output, cache_dir = Path(output), Path(cache_dir)
    output.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    frames, sources = [], []
    # Whole UTC days include local-midnight boundary hours. One request per year.
    first, last = start.date(), (end-timedelta(microseconds=1)).date()
    for year in range(first.year, last.year+1):
        lo, hi = max(first, date(year, 1, 1)), min(last, date(year, 12, 31))
        params = dict(stns=w['station'], vars='T:U:FH:Q', start=lo.strftime('%Y%m%d')+'01', end=hi.strftime('%Y%m%d')+'24')
        url = w['source'] + '?' + urlencode(params)
        key = hashlib.sha256(url.encode()).hexdigest()[:20]
        raw_path, meta_path = cache_dir/f'{key}.txt', cache_dir/f'{key}.json'
        if not raw_path.exists() or not meta_path.exists():
            if offline:
                raise ValueError(f'No cached KNMI response for {lo}..{hi}; run without --offline')
            with urlopen(Request(url, headers={'User-Agent': 'energy-management/0.1'}), timeout=90) as response:
                raw = response.read().decode('utf-8-sig')
            parse_knmi(raw, w['station'])  # reject server error pages before caching
            raw_path.write_text(raw)
            meta_path.write_text(json.dumps(dict(url=url, retrieved_at=datetime.now(UTC).isoformat()), indent=2))
        raw = raw_path.read_text()
        # KNMI may return a boundary day beyond the requested end. Trim each
        # request before concatenation; never deduplicate actual observations.
        request_start = datetime.combine(lo, datetime.min.time(), UTC).isoformat()
        request_end = datetime.combine(hi+timedelta(days=1), datetime.min.time(), UTC).isoformat()
        frames.append(parse_knmi(raw, w['station']).filter(
            (pl.col('interval_start_utc') >= request_start) &
            (pl.col('interval_start_utc') < request_end)))
        sources.append(dict(**json.loads(meta_path.read_text()), sha256=hashlib.sha256(raw.encode()).hexdigest()))
    frame = pl.concat(frames).filter((pl.col('interval_start_utc') >= start.isoformat()) & (pl.col('interval_start_utc') < end.isoformat())).sort('interval_start_utc')
    validate_weather(frame, start, end)
    frame.write_csv(output/'weather.txt', separator='\t')
    provenance = dict(schema_version=1, provider='KNMI observations', station=w['station'], station_name=w['station_name'], locality=w['locality'], timezone=w['timezone'], start_utc=start.isoformat(), end_utc=end.isoformat(), sources=sources,
                      conventions='UTC interval start = KNMI date + HH - 1 hour; T/U endpoint samples held over preceding hour; FH hourly mean; Q hourly accumulation J/cm2 divided by 360 to kWh/m2. No gap filling.')
    (output/'weather.json').write_text(json.dumps(provenance, indent=2)+'\n')
    return frame


def main():
    from .heat_pump import load_config
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--cache-dir', type=Path, default=Path('data/heat_pump_cache'))
    p.add_argument('--offline', action='store_true')
    a = p.parse_args()
    download_weather(load_config(a.config), a.output, a.cache_dir, a.offline)


if __name__ == '__main__':
    main()
