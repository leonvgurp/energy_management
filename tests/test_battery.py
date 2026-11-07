import polars as pl
import pytest

from energy_management.components.battery import dispatch


def settings():
    return dict(capacity_kwh=2., cost_eur=1500., charge_power_kw=1.,
                discharge_power_kw=1., charge_efficiency=.8, discharge_efficiency=.5)


def test_dispatch_power_efficiency_and_conservation():
    frame = pl.DataFrame({'grid_import_kwh': [0., 0., 0., 2., 2.],
                          'grid_export_kwh': [2., 2., 2., 0., 0.]})
    result = dispatch(frame, settings())
    assert result['battery_charge_kwh'].to_list() == pytest.approx([1, 1, .5, 0, 0])
    assert result['battery_discharge_kwh'].to_list() == pytest.approx([0, 0, 0, 1, 0])
    assert result['battery_soc_kwh'].to_list() == pytest.approx([.8, 1.6, 2, 0, 0])
    assert sum(result['battery_charge_kwh'])*.8 - sum(result['battery_discharge_kwh'])/.5 == pytest.approx(result['battery_soc_kwh'][-1])
    assert result['grid_export_kwh'].sum() == pytest.approx(3.5)
    assert result['grid_import_kwh'].sum() == pytest.approx(3.)


def test_invalid_battery():
    with pytest.raises(ValueError):
        dispatch(pl.DataFrame({'grid_import_kwh': [0.], 'grid_export_kwh': [0.]}), dict(settings(), charge_efficiency=2))
