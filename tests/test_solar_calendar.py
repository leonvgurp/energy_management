from datetime import date, datetime, timedelta
import io
import json
from zoneinfo import ZoneInfo

import pytest

from energy_management.solar import SolarConfig
from energy_management.solar_calendar import solar_calendar


def payload():
    zone = ZoneInfo("Europe/Amsterdam")
    days = []
    for i in range(366):
        day = date(2024, 1, 1)+timedelta(days=i)
        row = dict(date=day.isoformat())
        for name, hour in (("sunrise", 8), ("solar_noon", 13), ("sunset", 18)):
            row[name] = datetime(day.year, day.month, day.day, hour, tzinfo=zone).isoformat()
        days.append(row)
    return dict(tzid="Europe/Amsterdam", lat=52.1325, lng=5.43194, days=days)


def test_live_cache_then_offline_and_no_measurement_dependency(tmp_path, monkeypatch):
    import energy_management.solar_calendar as module
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        if "geocoding" in request.full_url:
            data = dict(results=[dict(name="Example", latitude=52.1325, longitude=5.43194,
                                      timezone="Europe/Amsterdam", country_code="NL")])
        else:
            data = payload()
        return io.StringIO(json.dumps(data))

    monkeypatch.setattr(module, "urlopen", fake_urlopen)
    config = SolarConfig("Example", (100,)*12, (20,)*12, 3.85)
    first, provenance = solar_calendar(config, 2024, tmp_path)
    second, same_provenance = solar_calendar(config, 2024, tmp_path, offline=True)
    assert len(calls) == 2 and first.height == 366 and first.equals(second)
    assert provenance == same_provenance
    assert provenance["location"]["timezone"] == "Europe/Amsterdam"
    with pytest.raises(ValueError, match="cached"):
        solar_calendar(config, 2025, tmp_path, offline=True)


@pytest.mark.parametrize("problem", ["missing", "duplicate", "offset", "coordinates", "polar"])
def test_invalid_provider_data_rejected(tmp_path, monkeypatch, problem):
    import energy_management.solar_calendar as module
    data = payload()
    if problem == "missing":
        data["days"].pop()
    elif problem == "duplicate":
        data["days"][-1] = data["days"][0]
    elif problem == "offset":
        data["days"][0]["sunrise"] = "2024-01-01T08:00:00+02:00"
    elif problem == "coordinates":
        data["lat"] = 10
    else:
        data["days"][0]["sunrise"] = None
    monkeypatch.setattr(module, "urlopen", lambda *args, **kwargs: io.StringIO(json.dumps(data)))
    config = SolarConfig("Example", (100,)*12, (20,)*12, 3.85, latitude=52.1325, longitude=5.43194)
    with pytest.raises(ValueError):
        solar_calendar(config, 2024, tmp_path)
