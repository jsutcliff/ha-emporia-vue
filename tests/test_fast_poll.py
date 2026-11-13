"""Tests for the 1-second power sensors and their configurable poll rate."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from pyemvue.device import (
    VueDevice,
    VueDeviceChannel,
    VueDeviceChannelUsage,
    VueUsageDevice,
)
from pyemvue.enums import Scale
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.data_entry_flow import FlowResultType

from custom_components.emporia_vue.const import (
    DOMAIN,
    ENABLE_1D,
    ENABLE_1M,
    ENABLE_1MON,
    ENABLE_1S,
    FAST_POLL_INTERVAL,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.usefixtures("enable_custom_integrations"),
]

DEVICE_GID = 1234
KWH_PER_SECOND = 0.0005  # 0.0005 kWh in one second == 1800 W


def make_device() -> VueDevice:
    device = VueDevice(gid=DEVICE_GID, modelNum="VUE003")
    device.device_name = "Panel"
    device.time_zone = "America/New_York"
    channel = VueDeviceChannel(gid=DEVICE_GID, name="Furnace", channelNum="1")
    channel.type = "Main"
    device.channels = [channel]
    return device


def make_usage(gids, instant, scale):
    usage_device = VueUsageDevice(gid=DEVICE_GID, timestamp=datetime.now(UTC))
    usage_device.channels["1"] = VueDeviceChannelUsage(
        gid=DEVICE_GID, usage=KWH_PER_SECOND, channelNum="1", name="Furnace"
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
            ENABLE_1M: False,
            ENABLE_1D: False,
            ENABLE_1MON: False,
            "solar_invert": True,
            **options,
        },
    )


def coordinator_1sec(hass, entry):
    return hass.data[DOMAIN][entry.entry_id]["coordinator_1sec"]


async def test_second_sensor_polls_at_configured_rate(hass, mock_vue):
    entry = make_entry(**{ENABLE_1S: True, FAST_POLL_INTERVAL: 2})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = coordinator_1sec(hass, entry)
    assert coordinator.update_interval == timedelta(seconds=2)
    scales = {call.args[2] for call in mock_vue.get_device_list_usage.call_args_list}
    assert scales == {Scale.SECOND.value}

    state = hass.states.get("sensor.furnace_power_1_second")
    assert state is not None
    assert float(state.state) == pytest.approx(1800.0)
    assert state.attributes["unit_of_measurement"] == "W"


async def test_second_sensor_disabled_by_default(hass, mock_vue):
    entry = make_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert coordinator_1sec(hass, entry) is None
    assert hass.states.get("sensor.furnace_power_1_second") is None


async def test_options_flow_changes_poll_rate(hass, mock_vue):
    entry = make_entry(**{ENABLE_1S: True, FAST_POLL_INTERVAL: 5})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {ENABLE_1S: True, FAST_POLL_INTERVAL: 1}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert coordinator_1sec(hass, entry).update_interval == timedelta(seconds=1)


async def test_options_flow_rejects_out_of_range(hass, mock_vue):
    entry = make_entry(**{ENABLE_1S: True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(Exception):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {ENABLE_1S: True, FAST_POLL_INTERVAL: 0}
        )


async def test_data_update_does_not_reload(hass, mock_vue):
    """Token refreshes update entry.data; that must not trigger a reload."""
    entry = make_entry(**{ENABLE_1S: True})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = coordinator_1sec(hass, entry)

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "refresh_token": "new"}
    )
    await hass.async_block_till_done()

    assert coordinator_1sec(hass, entry) is coordinator
