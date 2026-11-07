"""Load one self-contained, year-independent household scenario."""
import math
from pathlib import Path
import tomllib
from zoneinfo import ZoneInfo

from .components import boiler, general, heat_pump

# A reference calendar labels recurring month/day/hour scenarios; not a forecast.
REFERENCE_YEAR = 2001


def heat_pump_config(config, year):
    return dict(config['heat_pump'], weather=dict(timezone=config['scenario']['timezone'], years=[year], start='', end=''))


def general_config(config):
    """Adapt shared seed/single realization to the standalone model interface."""
    model = dict(config['general'], timezone=config['scenario']['timezone'])
    model['variability'] = dict(model['variability'], samples=0, seed=config['scenario']['seed'])
    return model


def load_config(path):
    with Path(path).open('rb') as stream:
        config = tomllib.load(stream)
    validate_config(config)
    return config


def validate_config(c):
    required = {'scenario', 'weather', 'solar', 'heat_pump', 'boiler', 'phev', 'general', 'plots'}
    if set(c) - {'costs'} != required:
        raise ValueError(f'Expected config sections: {sorted(required)}')
    if 'costs' in c:
        from .costs import validate
        validate(c['costs'])
    s, w = c['scenario'], c['weather']
    ZoneInfo(s['timezone'])
    if type(s['seed']) is not int or s['seed'] < 0:
        raise ValueError('scenario.seed must be a nonnegative integer')
    if not s['name'].strip():
        raise ValueError('scenario.name is required')
    def number(value, name, lo=-math.inf, hi=math.inf):
        if isinstance(value, bool) or not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f'{name} must be finite in [{lo}, {hi}]')
    for key in ('monthly_temperature_c', 'monthly_radiation_kwh_m2', 'monthly_sunshine_factors'):
        if len(w[key]) != 12:
            raise ValueError(f'weather.{key} requires twelve values, January first')
        for value in w[key]:
            number(value, key, lo=-math.inf if key == 'monthly_temperature_c' else 0)
    for key in ('temperature_offset_c', 'winter_temperature_offset_c'):
        number(w[key], key)
    for key in ('temperature_sigma_c', 'diurnal_temperature_amplitude_c', 'cloud_log_sigma', 'wind_m_s', 'sunshine_factor'):
        number(w[key], key, 0)
    for key in ('daily_correlation', 'temperature_hourly_correlation'):
        number(w[key], key, 0, .999)
    number(w['humidity_pct'], 'humidity_pct', 0, 100)
    number(w['latitude'], 'latitude', -66, 66)
    number(w['longitude'], 'longitude', -180, 180)
    if not w['winter_months'] or len(set(w['winter_months'])) != len(w['winter_months']) or any(type(m) is not int or not 1 <= m <= 12 for m in w['winter_months']):
        raise ValueError('winter_months must contain unique month numbers')
    if w['mode'] not in ('file', 'monthly_statistics') or (w['mode'] == 'file' and not w['file']):
        raise ValueError('Choose weather mode monthly_statistics, or file with a path')
    for name in ('boiler', 'phev', 'general'):
        if 'timezone' in c[name] or 'seed' in c[name]:
            raise ValueError(f'{name}: timezone/seed belong in scenario only')
    boiler.validate(dict(c['boiler'], timezone=s['timezone']))
    if {'seed', 'samples'} & set(c['general']['variability']):
        raise ValueError('Use scenario.seed; the annual runner generates one realization')
    general.validate(general_config(c))
    heat_pump.validate_config(heat_pump_config(c, REFERENCE_YEAR))
    if c['heat_pump']['scenario'] not in ('low', 'base', 'high'):
        raise ValueError('heat_pump.scenario must be low, base or high')
    # Explicitly reject calendar/date windows in an annual scenario.
    if 'weather' in c['heat_pump']:
        raise ValueError('Use the shared weather section')

    number(c['plots']['high_consumption_quantile'], 'high_consumption_quantile', 0, 1)
    if c['plots']['high_consumption_quantile'] == 0:
        raise ValueError('high_consumption_quantile must be greater than zero')
    for key in ('winter_months', 'summer_months'):
        months = c['plots'][key]
        if not months or len(set(months)) != len(months) or any(type(m) is not int or not 1 <= m <= 12 for m in months):
            raise ValueError(f'plots.{key} must contain unique month numbers')
    if set(c['plots']['winter_months']) & set(c['plots']['summer_months']):
        raise ValueError('Plot winter and summer months must not overlap')
    from matplotlib import colormaps
    if c['plots']['cmap'] not in colormaps:
        raise ValueError('plots.cmap must be a Matplotlib colormap name')
