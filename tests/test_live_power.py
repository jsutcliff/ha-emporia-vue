"""Tests for live power data, the update interval and circuit sensor options."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from pyemvue.device import (
    VueDevice,
    VueDeviceChannel,
    VueDeviceChannelUsage,
    VueUsageDevice,
)
from pyemvue.enums import Scale, Unit
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.emporia_vue.const import (
    CIRCUIT_SENSORS,
    DOMAIN,
    ENABLE_1D,
    ENABLE_1M,
    ENABLE_1MON,
    LIVE_POWER,
    PHASE_SENSORS,
    UPDATE_INTERVAL,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations"),
]

DEVICE_GID = 1234
# kWh used in one sample period; both work out to whole watts
USAGE_KWH = {
    Scale.SECOND.value: 0.0005,  # over 1 second  -> 1800 W
    Scale.MINUTE.value: 0.01,  # over 1 minute -> 600 W
}
MAINS = "1,2,3"


def make_device(name: str = "Panel") -> VueDevice:
    device = VueDevice(gid=DEVICE_GID, modelNum="VUE003")
    device.device_name = name
    device.time_zone = "America/New_York"
    channels = []
    for num, channel_name in ((MAINS, "Main"), ("1", "Furnace"), ("2", "")):
        channel = VueDeviceChannel(gid=DEVICE_GID, name=channel_name, channelNum=num)
        channel.type = "Main" if num == MAINS else "FiftyAmp"
        channels.append(channel)
    device.channels = channels
    return device


# Per-leg readings Emporia returns for Voltage / AmpHours requests
PHASE_USAGE = {
    Unit.VOLTS.value: {"Mains_A": 121.5, "Mains_B": 120.75},
    Unit.AMPHOURS.value: {"Mains_A": 0.005, "Mains_B": 0.0025},  # per second
}


def make_usage(gids, instant, scale, unit=Unit.KWH.value, **kwargs):
    usage_device = VueUsageDevice(gid=DEVICE_GID, timestamp=datetime.now(UTC))
    if unit == Unit.KWH.value:
        readings = {num: USAGE_KWH.get(scale, 1.0) for num in (MAINS, "1", "2")}
    else:
        readings = {MAINS: 1.0, "1": 1.0, **PHASE_USAGE[unit]}
    for num, value in readings.items():
        usage_device.channels[num] = VueDeviceChannelUsage(
            gid=DEVICE_GID, usage=value, channelNum=num
        )
    return {DEVICE_GID: usage_device}


@pytest.fixture
def mock_vue():
    vue = MagicMock()
    vue.login.return_value = True
    vue.get_devices.return_value = [make_device()]
    vue.get_device_list_usage.side_effect = make_usage
    with patch("custom_components.emporia_vue.PyEmVue", return_value=vue):
        yield vue


def make_entry(**options) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="123",
        data={
            "email": "user@example.com",
            "password": "pw",
            ENABLE_1M: True,
            ENABLE_1D: False,
            ENABLE_1MON: False,
            "solar_invert": True,
            **options,
        },
    )


async def setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def power_coordinator(hass, entry):
    return hass.data[DOMAIN][entry.entry_id]["coordinator_1min"]


def requested_scales(mock_vue) -> list[str]:
    return [call.args[2] for call in mock_vue.get_device_list_usage.call_args_list]


async def test_minute_average_by_default(hass, mock_vue):
    entry = make_entry()
    await setup(hass, entry)

    assert power_coordinator(hass, entry).update_interval == timedelta(minutes=1)
    assert set(requested_scales(mock_vue)) == {Scale.MINUTE.value}
    state = hass.states.get("sensor.main_power")
    assert float(state.state) == pytest.approx(600.0)
    assert state.attributes["unit_of_measurement"] == "W"


async def test_live_power_uses_same_entity_at_configured_rate(hass, mock_vue):
    entry = make_entry(**{LIVE_POWER: True, UPDATE_INTERVAL: 2})
    await setup(hass, entry)

    assert power_coordinator(hass, entry).update_interval == timedelta(seconds=2)
    # Live data for the sensor, minute data for the energy sensors
    assert set(requested_scales(mock_vue)) == {Scale.SECOND.value, Scale.MINUTE.value}
    assert float(hass.states.get("sensor.main_power").state) == pytest.approx(1800.0)

    entity_registry = er.async_get(hass)
    entity = entity_registry.async_get("sensor.main_power")
    assert entity.unique_id == f"sensor.emporia_vue.instant.{DEVICE_GID}-{MAINS}"


async def test_live_power_fetches_minute_data_once_a_minute(hass, mock_vue):
    entry = make_entry(**{LIVE_POWER: True, UPDATE_INTERVAL: 1})
    await setup(hass, entry)
    coordinator = power_coordinator(hass, entry)

    for _ in range(3):
        await coordinator.async_refresh()

    scales = requested_scales(mock_vue)
    assert scales.count(Scale.SECOND.value) == 4
    assert scales.count(Scale.MINUTE.value) == 1


async def test_options_flow_switches_mode_and_rate(hass, mock_vue):
    entry = make_entry()
    await setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CIRCUIT_SENSORS: True, LIVE_POWER: True, UPDATE_INTERVAL: 1},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert power_coordinator(hass, entry).update_interval == timedelta(seconds=1)
    # Same entity, now showing live data
    assert float(hass.states.get("sensor.main_power").state) == pytest.approx(1800.0)


async def test_options_flow_rejects_out_of_range(hass, mock_vue):
    entry = make_entry()
    await setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(Exception):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CIRCUIT_SENSORS: True, LIVE_POWER: True, UPDATE_INTERVAL: 0},
        )


async def test_data_update_does_not_reload(hass, mock_vue):
    """Token refreshes update entry.data; that must not trigger a reload."""
    entry = make_entry(**{LIVE_POWER: True})
    await setup(hass, entry)
    coordinator = power_coordinator(hass, entry)

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "refresh_token": "new"}
    )
    await hass.async_block_till_done()

    assert power_coordinator(hass, entry) is coordinator


async def test_circuit_sensors_can_be_turned_off(hass, mock_vue):
    entry = make_entry()
    await setup(hass, entry)
    assert hass.states.get("sensor.furnace_power") is not None
    device_registry = dr.async_get(hass)
    assert device_registry.async_get_device_by_identifier((DOMAIN, f"{DEVICE_GID}-1"), entry.entry_id)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CIRCUIT_SENSORS: False, LIVE_POWER: False, UPDATE_INTERVAL: 5},
    )
    await hass.async_block_till_done()

    assert hass.states.get("sensor.main_power") is not None
    assert hass.states.get("sensor.furnace_power") is None
    assert er.async_get(hass).async_get("sensor.furnace_power") is None
    assert device_registry.async_get_device_by_identifier((DOMAIN, f"{DEVICE_GID}-1"), entry.entry_id) is None
    assert device_registry.async_get_device_by_identifier((DOMAIN, f"{DEVICE_GID}-{MAINS}"), entry.entry_id)


async def test_old_one_second_entities_are_removed(hass, mock_vue):
    entry = make_entry()
    entry.add_to_hass(hass)
    entity_registry = er.async_get(hass)
    old = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"sensor.emporia_vue.1S.{DEVICE_GID}-{MAINS}",
        config_entry=entry,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entity_registry.async_get(old.entity_id) is None


async def test_unnamed_monitor_uses_model_for_circuit_names(hass, mock_vue):
    mock_vue.get_devices.return_value = [make_device(name=None)]
    entry = make_entry()
    await setup(hass, entry)

    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, f"{DEVICE_GID}-2"), entry.entry_id)
    assert device.name == "VUE003 Circuit 2"


def requested_units(mock_vue) -> set[str]:
    return {
        call.args[3] if len(call.args) > 3 else Unit.KWH.value
        for call in mock_vue.get_device_list_usage.call_args_list
    }


async def test_phase_sensors_off_by_default(hass, mock_vue):
    entry = make_entry(**{LIVE_POWER: True})
    await setup(hass, entry)

    assert requested_units(mock_vue) == {Unit.KWH.value}
    assert hass.states.get("sensor.main_voltage_phase_a") is None


async def test_live_phase_voltage_and_current(hass, mock_vue):
    entry = make_entry(**{LIVE_POWER: True, UPDATE_INTERVAL: 2, PHASE_SENSORS: True})
    await setup(hass, entry)

    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator_phase"]
    assert coordinator.update_interval == timedelta(seconds=2)
    phase_calls = [
        call for call in mock_vue.get_device_list_usage.call_args_list
        if len(call.args) > 3 and call.args[3] != Unit.KWH.value
    ]
    assert {call.args[2] for call in phase_calls} == {Scale.SECOND.value}

    volts_a = hass.states.get("sensor.main_voltage_phase_a")
    assert float(volts_a.state) == pytest.approx(121.5)
    assert volts_a.attributes["unit_of_measurement"] == "V"
    assert float(hass.states.get("sensor.main_voltage_phase_b").state) == pytest.approx(120.75)
    amps_a = hass.states.get("sensor.main_current_phase_a")
    assert float(amps_a.state) == pytest.approx(18.0)  # 0.005 Ah in 1 s
    assert amps_a.attributes["unit_of_measurement"] == "A"
    assert float(hass.states.get("sensor.main_current_phase_b").state) == pytest.approx(9.0)

    # Attached to the mains device, which keeps its name
    entity = er.async_get(hass).async_get("sensor.main_voltage_phase_a")
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device.name == "Main"
    # Only the legs, not the other channels in the response
    phase_entities = [
        e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if ".voltage." in e.unique_id or ".current." in e.unique_id
    ]
    assert len(phase_entities) == 4


async def test_minute_phase_current_conversion(hass, mock_vue):
    entry = make_entry(**{PHASE_SENSORS: True})
    await setup(hass, entry)

    # 0.005 Ah in one minute -> 0.3 A
    assert float(hass.states.get("sensor.main_current_phase_a").state) == pytest.approx(0.3)


async def test_phase_warning_when_no_legs_returned(hass, mock_vue, caplog):
    with patch.dict(
        PHASE_USAGE,
        {Unit.VOLTS.value: {}, Unit.AMPHOURS.value: {}},
    ):
        entry = make_entry(**{PHASE_SENSORS: True})
        await setup(hass, entry)

    assert "no per-leg voltage channels" in caplog.text
    assert hass.states.get("sensor.main_voltage_phase_a") is None
    assert hass.states.get("sensor.main_power") is not None
