"""Assemble the saved household inputs without modifying any source files.

Run from the repository root through Pixi. Private values remain in ignored data/.
"""
import hashlib
import json
from pathlib import Path
import re
import tomllib

import polars as pl
from energy_management.solar_comparison import read_daily_yields


def main():
    root = Path('data/overall')
    root.mkdir(parents=True, exist_ok=True)
    text = Path('configs/household.example.toml').read_text()
    solar = tomllib.loads(Path('outputs/solar_statistics_2025_seed42/input_statistics.toml').read_text())
    for key in ('monthly_kwh', 'max_daily_kwh', 'max_power_kw'):
        text = re.sub(rf'^{key} = .*$', f'{key} = {json.dumps(solar[key])}', text, flags=re.M)
    # Use the same measured years for PV and reference irradiation. Expand each
    # observed deviation from mean annual PV by 10%, not ten percentage points.
    daily = read_daily_yields('data/hourly_yield_2023_2025.txt')
    annual = daily.group_by(pl.col('date').dt.year().alias('year')).agg(pl.col('daily_kwh').sum()).sort('year')
    if annual['year'].to_list() != [2023, 2024, 2025]:
        raise ValueError('PV calibration requires complete 2023–2025 measurements')
    values = annual['daily_kwh'].to_numpy()
    mean = float(values.mean())
    history = pl.read_csv('data/weather_history_2015_2025/weather.txt', separator='\t')
    reference = (history.with_columns(pl.col('interval_start_utc').str.to_datetime(time_zone='UTC')
                 .dt.convert_time_zone('Europe/Amsterdam').alias('local'))
                 .filter(pl.col('local').dt.year().is_in([2023, 2024, 2025]))
                 .group_by(pl.col('local').dt.year().alias('year'), pl.col('local').dt.month().alias('month'))
                 .agg(pl.col('radiation_kwh_m2').sum())
                 .group_by('month').agg(pl.col('radiation_kwh_m2').mean()).sort('month'))
    response = dict(reference_monthly_radiation_kwh_m2=reference['radiation_kwh_m2'].to_list(),
                    annual_min_factor=max(0., 1+1.1*(float(values.min())/mean-1)),
                    annual_max_factor=1+1.1*(float(values.max())/mean-1))
    section = '[solar.weather_response]\n'+''.join(f'{k} = {json.dumps(v)}\n' for k,v in response.items())+'\n'
    text = text.replace('[heat_pump]\n', section+'[heat_pump]\n')
    (root/'solar_weather_calibration.json').write_text(json.dumps(dict(
        years=annual.to_dicts(), mean_annual_kwh=mean, bandwidth_extension_fraction=.1,
        response=response, source_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path('data/hourly_yield_2023_2025.txt'), Path('data/weather_history_2015_2025/weather.txt')]}), indent=2)+'\n')
    hp = Path('data/heat_pump.toml').read_text().split('[house]', 1)[1]
    hp = '[heat_pump.house]' + hp
    hp = re.sub(r'^\[(operation|performance|scenarios\.[^\]]+|manufacturer_reference)\]', r'[heat_pump.\1]', hp, flags=re.M)
    text = text[:text.index('[heat_pump.house]')] + hp + '\n\n' + text[text.index('[boiler]'):]
    car = tomllib.loads(Path('data/car/schedule.toml').read_text())
    for key, value in car.items():
        if key not in ('timezone', 'seed'):
            text = re.sub(rf'^{key} = .*$', f'{key} = {json.dumps(value)}', text, flags=re.M)
    general = json.loads(Path('outputs/regular_usage_2026/run.json').read_text())['config']
    general_text = '[general]\nannual_total_kwh = 2500.0\n'
    for section, values in general.items():
        if isinstance(values, dict):
            general_text += f'\n[general.{section}]\n'
            general_text += ''.join(f'{key} = {json.dumps(value)}\n' for key, value in values.items()
                                    if key not in ('samples', 'seed'))
    text = text[:text.index('[general]')] + general_text + '\n' + text[text.index('[plots]'):]
    text = text.replace('illustrative_household', 'saved_household_bootstrap')
    text = text.replace('mode = "monthly_statistics"', 'mode = "file"')
    text = text.replace('file = ""', 'file = "weather_is_set_per_member.txt"')
    text = text.replace('latitude = 52.1', 'latitude = 52.1325').replace('longitude = 5.18', 'longitude = 5.43194')
    text = text.replace('Illustrative central Netherlands', 'Leusden')
    text = text.replace('# Illustrative full January–December scenario, NOT household calibration.',
        '# Saved household component inputs, assembled for bootstrap analysis; not calibrated.')
    (root/'household.analysis.toml').write_text(text)
    sampling = Path('configs/weather_scenarios.example.toml').read_text().replace('members = 30', 'members = 100')
    (root/'weather.analysis.toml').write_text(sampling)


if __name__ == '__main__':
    main()
