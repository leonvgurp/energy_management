"""Annual electricity billing on complete hourly grid flows, in consumer EUR.

Rates must already include applicable taxes/VAT. Export remuneration can instead
be a consumer cash receipt without VAT; never automatically add 21% to it.
"""
from datetime import datetime
import math
from zoneinfo import ZoneInfo

import numpy as np

from .calendar import calendar_hours


def validate(config):
    required = {'name', 'saldering', 'meter', 'import_single_eur_kwh',
                'import_normal_eur_kwh', 'import_offpeak_eur_kwh',
                'export_eur_kwh', 'offpeak_start_hour', 'offpeak_end_hour',
                'offpeak_dates', 'netting_allocation', 'fixed', 'export_charges'}
    if set(config) != required:
        raise ValueError(f'costs requires exactly {sorted(required)}')
    if type(config['saldering']) is not bool:
        raise ValueError('costs.saldering must be boolean')
    if config['meter'] not in ('single', 'dual'):
        raise ValueError('costs.meter must be single or dual')
    if config['netting_allocation'] not in ('proportional', 'normal_first', 'offpeak_first'):
        raise ValueError('Unknown netting_allocation')
    def finite(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Cost rates and thresholds must be finite numbers')
    for key in ('import_single_eur_kwh', 'import_normal_eur_kwh', 'import_offpeak_eur_kwh', 'export_eur_kwh'):
        finite(config[key])
    for key in ('offpeak_start_hour', 'offpeak_end_hour'):
        if type(config[key]) is not int or not 0 <= config[key] <= 23:
            raise ValueError('Offpeak hours must be integers in 0..23')
    for day in config['offpeak_dates']:
        if datetime.strptime(day, '%m-%d').strftime('%m-%d') != day:
            raise ValueError('offpeak_dates must be MM-DD')
    for line in config['fixed']:
        if set(line) != {'name', 'eur_day', 'eur_year'}:
            raise ValueError('Fixed lines require name, eur_day, eur_year')
        finite(line['eur_day'])
        finite(line['eur_year'])
    for line in config['export_charges']:
        if set(line) != {'name', 'basis', 'eur_kwh', 'eur_day', 'eur_year', 'tiers'}:
            raise ValueError('Export charges require name, basis, eur_kwh, eur_day, eur_year, tiers')
        if line['basis'] not in ('gross_export', 'excess_export'):
            raise ValueError('Export charge basis must be gross_export or excess_export')
        for key in ('eur_kwh', 'eur_day', 'eur_year'):
            finite(line[key])
        previous = 0
        for tier in line['tiers']:
            if set(tier) != {'up_to_kwh', 'eur_year'}:
                raise ValueError('Tiers require up_to_kwh and eur_year')
            finite(tier['up_to_kwh'])
            finite(tier['eur_year'])
            if tier['up_to_kwh'] <= previous:
                raise ValueError('Tier upper bounds must strictly increase above zero')
            previous = tier['up_to_kwh']


def annual_cost(hourly, config, timezone_name='Europe/Amsterdam', saldering=None):
    """Bill one complete calendar year. Tiers select ONE annual fee, not marginal fees.

    Dual-meter annual netting crosses registers with an explicit credit-allocation
    policy. This is an assumption to check against the actual contract.
    """
    validate(config)
    enabled = config['saldering'] if saldering is None else saldering
    if type(enabled) is not bool:
        raise ValueError('saldering must be boolean')
    zone = ZoneInfo(timezone_name)
    stamps = [datetime.fromisoformat(t) for t in hourly['interval_start_utc']]
    if not stamps or any(t.tzinfo is None for t in stamps):
        raise ValueError('Require timezone-aware full-year hourly timestamps')
    local = [t.astimezone(zone) for t in stamps]
    year = local[0].year
    expected, _ = calendar_hours(year, timezone_name)
    if stamps != list(expected):
        raise ValueError('Costs require ordered, unique, complete calendar-year hours')
    flows = [hourly[key].to_numpy().astype(float) for key in ('grid_import_kwh', 'grid_export_kwh')]
    if any(not np.isfinite(a).all() or (a < 0).any() for a in flows):
        raise ValueError('Grid energy must be nonnegative, finite and nonmissing')
    imports, exports = flows
    start, end = config['offpeak_start_hour'], config['offpeak_end_hour']
    offpeak = np.array([t.weekday() >= 5 or t.strftime('%m-%d') in config['offpeak_dates'] or
                       (t.hour >= start or t.hour < end if start > end else start <= t.hour < end)
                       for t in local])
    quantities = np.array([imports[~offpeak].sum(), imports[offpeak].sum()])
    rates = np.array([config['import_normal_eur_kwh'], config['import_offpeak_eur_kwh']])
    if config['meter'] == 'single':
        rates[:] = config['import_single_eur_kwh']
    total_import, total_export = float(imports.sum()), float(exports.sum())
    netted = min(total_import, total_export) if enabled else 0.0
    allocation = config['netting_allocation']
    if allocation == 'proportional':
        credits = quantities * (netted / total_import if total_import else 0)
    else:
        first = 0 if allocation == 'normal_first' else 1
        credits = np.zeros(2)
        credits[first] = min(quantities[first], netted)
        credits[1-first] = netted - credits[first]
    excess = total_export - netted
    days = len({t.date() for t in local})
    lines = [dict(name='grid_import', eur=float(quantities @ rates)),
             dict(name='saldering_credit', eur=-float(credits @ rates)),
             dict(name='export_remuneration', eur=-excess*config['export_eur_kwh'])]
    for line in config['fixed']:
        lines.append(dict(name=line['name'], eur=days*line['eur_day']+line['eur_year']))
    for line in config['export_charges']:
        quantity = total_export if line['basis'] == 'gross_export' else excess
        fee = 0.0
        if quantity > 0:
            fee = quantity*line['eur_kwh'] + days*line['eur_day'] + line['eur_year']
            if line['tiers']:
                match = next((t for t in line['tiers'] if quantity <= t['up_to_kwh']), None)
                if match is None:
                    raise ValueError(f"Export exceeds final tier: {line['name']}")
                fee += match['eur_year']
        lines.append(dict(name=line['name'], eur=fee, basis_kwh=quantity))
    return dict(contract=config['name'], saldering=enabled, calendar_year=year, days=days,
                grid_import_kwh=total_import, grid_export_kwh=total_export,
                netted_kwh=netted, remunerated_export_kwh=excess,
                lines=lines, total_eur=sum(line['eur'] for line in lines))


def comparison(hourly, config, timezone_name='Europe/Amsterdam'):
    """Isolate the policy effect with identical flows, calendar and tariffs."""
    on = annual_cost(hourly, config, timezone_name, True)
    off = annual_cost(hourly, config, timezone_name, False)
    return dict(selected=on if config['saldering'] else off, with_saldering=on,
                without_saldering=off, without_minus_with_eur=off['total_eur']-on['total_eur'])
