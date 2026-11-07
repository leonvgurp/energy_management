"""Hourly solar-surplus battery dispatch, with AC-side power and energy limits."""
import math

import numpy as np
import polars as pl


def validate(config):
    required = {'capacity_kwh', 'cost_eur', 'charge_power_kw', 'discharge_power_kw', 'charge_efficiency', 'discharge_efficiency'}
    if set(config) != required:
        raise ValueError(f'battery requires exactly {sorted(required)}')
    for key, value in config.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f'battery {key} must be finite')
        if value < 0 or (key != 'cost_eur' and value == 0):
            raise ValueError(f'battery {key} must be positive (cost may be zero)')
    if config['charge_efficiency'] > 1 or config['discharge_efficiency'] > 1:
        raise ValueError('battery efficiencies must be in (0, 1]')


def dispatch(hourly, config):
    """Start empty; charge from surplus, discharge into deficits, never grid charge.

    Each row represents one hour. Energy entering/leaving the battery is measured
    at the AC bus; state of charge is stored energy at the end of the interval.
    """
    validate(config)
    surplus = hourly['grid_export_kwh'].to_numpy().astype(float)
    deficit = hourly['grid_import_kwh'].to_numpy().astype(float)
    if not np.isfinite(surplus).all() or not np.isfinite(deficit).all() or (surplus < 0).any() or (deficit < 0).any() or ((surplus > 0) & (deficit > 0)).any():
        raise ValueError('Battery requires finite, nonnegative, exclusive hourly grid flows')
    charge, discharge, state = (np.zeros(len(surplus)) for _ in range(3))
    soc = 0.0
    for i, (export, imported) in enumerate(zip(surplus, deficit)):
        charge[i] = min(export, config['charge_power_kw'], (config['capacity_kwh']-soc)/config['charge_efficiency'])
        soc += charge[i]*config['charge_efficiency']
        discharge[i] = min(imported, config['discharge_power_kw'], soc*config['discharge_efficiency'])
        soc -= discharge[i]/config['discharge_efficiency']
        state[i] = soc
    return hourly.with_columns(
        pl.Series('battery_charge_kwh', charge), pl.Series('battery_discharge_kwh', discharge),
        pl.Series('battery_soc_kwh', state),
        pl.Series('grid_import_kwh', deficit-discharge), pl.Series('grid_export_kwh', surplus-charge))
