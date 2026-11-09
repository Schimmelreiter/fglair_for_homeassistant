"""Upstream meter: energy per operating mode and hvac_action from the measured power."""

from unittest.mock import MagicMock

from homeassistant.components.climate.const import HVACAction, HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.fglair_heatpump_controller.climate import FujitsuClimate
from custom_components.fglair_heatpump_controller.const import (
    CONF_ACTION_THRESHOLD,
    CONF_METERS,
    DEFAULT_TEMPERATURE_OFFSET,
    DEFAULT_TOKEN_PATH,
    DOMAIN,
)
from custom_components.fglair_heatpump_controller.energy import UpstreamMeter
from custom_components.fglair_heatpump_controller.sensor import _EnergyManager

E = "sensor.plug_energy"
P = "sensor.plug_power"


def _plug(hass: HomeAssistant) -> None:
    """Smart plug with energy counter and power sensor on one device."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "plug")}
    )
    reg = er.async_get(hass)
    reg.async_get_or_create(
        "sensor",
        "test",
        "e",
        device_id=dev.id,
        suggested_object_id="plug_energy",
        original_device_class="energy",
    )
    reg.async_get_or_create(
        "sensor",
        "test",
        "p",
        device_id=dev.id,
        suggested_object_id="plug_power",
        original_device_class="power",
    )
    hass.states.async_set(
        E, "100.0", {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    hass.states.async_set(P, "5", {"unit_of_measurement": "W", "device_class": "power"})


async def test_energy_split_by_mode(hass: HomeAssistant) -> None:
    _plug(hass)
    meter = UpstreamMeter(hass, "dsn1", E, 150)
    mode = {"m": HVACMode.HEAT, "t": 22.0, "c": 19.0}
    meter.mode_provider = lambda: (mode["m"], mode["t"], mode["c"])
    meter.async_start()
    assert meter.energy_id == E and meter.power_id == P
    meter.restore_last_energy(None)  # starting point = current reading
    got: list = []
    meter.add_energy_listener(lambda b, k: got.append((b, round(k, 4))))

    hass.states.async_set(E, "100.5", {"unit_of_measurement": "kWh"})
    mode["m"] = HVACMode.COOL
    hass.states.async_set(E, "100.75", {"unit_of_measurement": "kWh"})
    mode["m"] = HVACMode.OFF
    hass.states.async_set(E, "100.76", {"unit_of_measurement": "kWh"})
    hass.states.async_set(E, "unavailable", {})
    mode["m"] = HVACMode.FAN_ONLY
    hass.states.async_set(
        E, "100.86", {"unit_of_measurement": "kWh"}
    )  # continues after the gap
    mode["m"] = HVACMode.DRY
    hass.states.async_set(E, "0.2", {"unit_of_measurement": "kWh"})  # counter reset
    hass.states.async_set(E, "50", {"unit_of_measurement": "kWh"})  # implausible step
    # auto: room colder than target -> heat, warmer -> cool
    mode["m"] = HVACMode.AUTO
    hass.states.async_set(E, "50.1", {"unit_of_measurement": "kWh"})
    mode["c"] = 24.0
    hass.states.async_set(E, "50.3", {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()
    assert got == [
        ("heat", 0.5),
        ("cool", 0.25),
        ("standby", 0.01),
        ("fan", 0.1),
        ("dry", 0.2),
        ("heat", 0.1),
        ("cool", 0.2),
    ]
    meter.async_stop()


async def test_restore_counts_downtime(hass: HomeAssistant) -> None:
    _plug(hass)
    meter = UpstreamMeter(hass, "dsn1", E, 150)
    meter.mode_provider = lambda: (HVACMode.HEAT, 22.0, 20.0)
    meter.async_start()
    meter.restore_last_energy(99.0)  # reading stored before the restart
    got: list = []
    meter.add_energy_listener(lambda b, k: got.append((b, round(k, 4))))
    hass.states.async_set(E, "100.2", {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()
    assert got == [("heat", 1.2)]


async def test_power_only_integrates(hass: HomeAssistant, freezer) -> None:
    hass.states.async_set(
        P, "1000", {"unit_of_measurement": "W", "device_class": "power"}
    )
    meter = UpstreamMeter(hass, "dsn1", P, 150)
    meter.mode_provider = lambda: (HVACMode.HEAT, 22.0, 20.0)
    meter.async_start()
    assert meter.energy_id is None and meter.power_id == P and meter.running() is True
    got: list = []
    meter.add_energy_listener(lambda b, k: got.append((b, round(k, 4))))
    freezer.tick(1800)
    hass.states.async_set(P, "10", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert got == [("heat", 0.5)]
    assert meter.running() is False


def _climate(
    hass: HomeAssistant, mode_value: int, desc: str, op: str = "Normal"
) -> FujitsuClimate:
    c = FujitsuClimate(
        fglair_api_client=MagicMock(),
        dsn="dsn1",
        region="eu",
        tokenpath=DEFAULT_TOKEN_PATH,
        temperature_offset=DEFAULT_TEMPERATURE_OFFSET,
        hass=hass,
        coordinator=MagicMock(),
    )
    c._fujitsu_device.get_operation_mode = MagicMock(return_value={"value": mode_value})
    c._fujitsu_device.get_operation_mode_desc = MagicMock(return_value=desc)
    c._fujitsu_device.get_op_status_desc = MagicMock(return_value=op)
    return c


async def test_hvac_action_from_power(hass: HomeAssistant) -> None:
    _plug(hass)
    meter = UpstreamMeter(hass, "dsn1", E, 150)
    meter.async_start()
    hass.data.setdefault(DOMAIN, {})["meters"] = {"dsn1": meter}
    heat = _climate(hass, 1, "heat")
    meter.mode_provider = lambda: (heat.hvac_mode, 22.0, 20.0)
    assert heat.hvac_mode == HVACMode.HEAT
    meter.power = 5
    assert heat.hvac_action == HVACAction.IDLE
    meter.power = 900
    assert heat.hvac_action == HVACAction.HEATING
    assert _climate(hass, 1, "heat", op="Defrost").hvac_action == HVACAction.PREHEATING
    cool = _climate(hass, 1, "cool")
    assert cool.hvac_action == HVACAction.COOLING
    fan = _climate(hass, 1, "fan_only")
    meter.power = 70
    assert fan.hvac_action == HVACAction.FAN
    auto = _climate(hass, 1, "auto")
    meter.mode_provider = lambda: (HVACMode.AUTO, 22.0, 25.0)
    meter.power = 400
    assert auto.hvac_action == HVACAction.COOLING
    meter.mode_provider = lambda: (HVACMode.AUTO, 22.0, 19.0)
    assert auto.hvac_action == HVACAction.HEATING
    # without meter: as before (mode only)
    hass.data[DOMAIN]["meters"] = {}
    meter.power = 5
    assert heat.hvac_action == HVACAction.HEATING


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow(hass: HomeAssistant) -> None:
    _plug(hass)
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={CONF_METERS: {"dsn2": P}})
    entry.add_to_hass(hass)
    coord = MagicMock()
    coord.devices = ["dsn1", "dsn2"]
    hass.data[DOMAIN] = {
        entry.entry_id: coord,
        "properties": {
            "dsn1": ("Schlafzimmer", [], {}),
            "dsn2": ("Wohnzimmer", [], {}),
        },
    }
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] == "form"
    keys = [str(k) for k in r["data_schema"].schema]
    assert keys == ["Schlafzimmer", "Wohnzimmer", CONF_ACTION_THRESHOLD]
    r = await hass.config_entries.options.async_configure(
        r["flow_id"], {"Schlafzimmer": E, CONF_ACTION_THRESHOLD: 200}
    )
    assert r["type"] == "create_entry"
    assert entry.options[CONF_METERS] == {"dsn1": E}
    assert entry.options[CONF_ACTION_THRESHOLD] == 200


async def test_energy_sensors(hass: HomeAssistant) -> None:
    _plug(hass)
    meter = UpstreamMeter(hass, "dsn1", E, 150)
    meter.mode_provider = lambda: (HVACMode.COOL, 22.0, 25.0)
    meter.async_start()
    hass.data.setdefault(DOMAIN, {})["meters"] = {"dsn1": meter}
    added: list = []
    mgr = _EnergyManager(hass, lambda ents: added.extend(ents))
    mgr.async_update("dsn1", "Schlafzimmer", [], {})
    mgr.async_update("dsn1", "Schlafzimmer", [], {})  # only once
    mgr.async_update("dsn9", "ohne Zaehler", [], {})
    assert sorted(e.unique_id for e in added) == sorted(
        f"dsn1_energy_{b}" for b in ("heat", "cool", "dry", "fan", "standby", "total")
    )
    for e in added:
        meter.add_energy_listener(e._async_add)
    meter.restore_last_energy(None)
    hass.states.async_set(E, "100.4", {"unit_of_measurement": "kWh"})
    await hass.async_block_till_done()
    werte = {e.unique_id.split("_energy_")[1]: e.native_value for e in added}
    assert werte == {
        "heat": 0.0,
        "cool": 0.4,
        "dry": 0.0,
        "fan": 0.0,
        "standby": 0.0,
        "total": 0.4,
    }
    total = next(e for e in added if e.unique_id.endswith("total"))
    total._update_attrs()
    assert total.extra_state_attributes["source_reading"] == 100.4
