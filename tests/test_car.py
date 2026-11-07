import pytest
from energy_management.car import scenarios


def config():
    return dict(usable_battery_kwh=10., reference_range_km=50., round_trip_km=30.,
                charging_efficiency=.8, electricity_eur_kwh=.3, ere_eur_kwh=.1,
                ere_eligible_fraction=.5, consumption_factors=[1., 2.])


def test_energy_losses_credit_and_range_limit():
    a, b = scenarios(config()).to_dicts()
    assert a['battery_kwh'] == pytest.approx(6.)
    assert a['meter_kwh'] == pytest.approx(7.5)
    assert a['net_electricity_eur'] == pytest.approx(1.875)
    assert b['electric_km'] == pytest.approx(25.)
    assert b['distance_beyond_battery_km'] == pytest.approx(5.)
    assert b['battery_kwh'] == pytest.approx(10.)


@pytest.mark.parametrize('key,value', [('charging_efficiency',0), ('ere_eligible_fraction',1.1),
    ('usable_battery_kwh',float('nan')), ('consumption_factors',[]), ('consumption_factors',[-1])])
def test_invalid_inputs(key, value):
    c = config()
    c[key] = value
    with pytest.raises(ValueError):
        scenarios(c)
