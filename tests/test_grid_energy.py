"""Tests for splitting net mains energy into grid import and export."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from pyemvue.device import VueDevice, VueDeviceChannel, VueUsageDevice
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emporia_vue.const import DOMAIN
from custom_components.emporia_vue.grid import MINUTE, floor_minute, split_minutes

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

GID = 1234
NOW = datetime(2026, 11, 13, 12, 0, 30, tzinfo=UTC)
START = datetime(2026, 11, 13, 11, 56, tzinfo=UTC)


def test_split_positive_is_import_negative_is_export():
    imported, exported, last = split_minutes([0.01, -0.02, 0.005, -0.001], START, None, NOW)
    assert imported == pytest.approx(0.015)
    assert exported == pytest.approx(0.021)
    assert last == START + 3 * MINUTE


def test_split_skips_counted_and_incomplete_minutes():
    # 12:00 is still in progress; 11:56 was already counted
    values = [0.5, 0.01, 0.02, 0.03, 9.9]
    imported, _, last = split_minutes(values, START, START, NOW)
    assert imported == pytest.approx(0.06)
    assert last == START + 3 * MINUTE


def test_split_waits_for_missing_recent_minute():
    imported, _, last = split_minutes([0.01, None, 0.02], START, None, NOW)
    assert imported == pytest.approx(0.01)
    assert last == START


def test_split_gives_up_on_old_missing_minute():
    old = NOW - timedelta(hours=1)
    imported, _, last = split_minutes([0.01, None, 0.02], floor_minute(old), None, NOW)
    assert imported == pytest.approx(0.03)
    assert last == floor_minute(old) + 2 * MINUTE


def make_vue(chart):
    device = VueDevice(gid=GID, modelNum="VUE003")
    device.device_name = "Panel"
    device.time_zone = "America/New_York"
    mains = VueDeviceChannel(gid=GID, name="Main", channelNum="1,2,3")
    mains.type = "Main"
    device.channels = [mains]
    vue = MagicMock()
    vue.login.return_value = True
    vue.get_devices.return_value = [device]
    vue.get_device_list_usage.return_value = {GID: VueUsageDevice(gid=GID, timestamp=datetime.now(UTC))}
    vue.get_chart_usage.side_effect = chart
    return vue


def make_entry():
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="123",
        data={
            "email": "user@example.com",
            "password": "pw",
            "enable_1m": False,
            "enable_1d": False,
            "enable_1mon": False,
        },
    )


async def test_grid_sensors_backfill_after_restart(hass, hass_storage):
    entry = make_entry()
    entry.add_to_hass(hass)
    last = floor_minute(datetime.now(UTC)) - 4 * MINUTE
    hass_storage[f"{DOMAIN}.grid_energy.{entry.entry_id}"] = {
        "version": 1,
        "key": f"{DOMAIN}.grid_energy.{entry.entry_id}",
        "data": {str(GID): {"import_kwh": 10.0, "export_kwh": 2.0, "last_minute": last.isoformat()}},
    }
    requested = []

    def chart(channel, start, end, scale):
        requested.append((channel.channel_num, start))
        return [0.01, -0.02, 0.005], start

    with patch("custom_components.emporia_vue.PyEmVue", return_value=make_vue(chart)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert requested == [("1,2,3", last + MINUTE)]
    imported = hass.states.get("sensor.main_grid_import_energy")
    exported = hass.states.get("sensor.main_grid_export_energy")
    assert float(imported.state) == pytest.approx(10.015)
    assert float(exported.state) == pytest.approx(2.02)
    assert imported.attributes["state_class"] == "total_increasing"
    assert imported.attributes["unit_of_measurement"] == "kWh"
    saved = hass_storage[f"{DOMAIN}.grid_energy.{entry.entry_id}"]["data"][str(GID)]
    assert saved["import_kwh"] == pytest.approx(10.015)


async def test_grid_sensors_start_at_zero(hass):
    entry = make_entry()
    entry.add_to_hass(hass)
    vue = make_vue(lambda *args: ([], None))
    with patch("custom_components.emporia_vue.PyEmVue", return_value=vue):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert float(hass.states.get("sensor.main_grid_import_energy").state) == 0
    assert float(hass.states.get("sensor.main_grid_export_energy").state) == 0
    vue.get_chart_usage.assert_not_called()
