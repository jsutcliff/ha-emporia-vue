"""Grid import/export energy for the Energy dashboard.

Emporia reports the mains as net energy (import minus export), which the
Energy dashboard can't use. This splits Emporia's per-minute net mains energy
into two ever-increasing totals: positive minutes are imported from the grid,
negative minutes (solar flowing back) are exported to it.

Minutes come from Emporia's chart history, so minutes missed while Home
Assistant was down are filled in on the next update (up to MAX_BACKFILL).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from typing import Any

from pyemvue import PyEmVue
from pyemvue.device import VueDeviceChannel
from pyemvue.enums import Scale

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import UpdateFailed

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MAINS_CHANNEL = "1,2,3"
MINUTE = timedelta(minutes=1)
MAX_BACKFILL = timedelta(hours=6)
# A minute Emporia still has no value for after this long is counted as zero
GIVE_UP_AFTER = timedelta(minutes=15)
STORAGE_VERSION = 1


def floor_minute(moment: datetime) -> datetime:
    """Return the start of the minute containing moment."""
    return moment.replace(second=0, microsecond=0)


def split_minutes(
    values: list[float | None],
    first_minute: datetime,
    after: datetime | None,
    now: datetime,
) -> tuple[float, float, datetime | None]:
    """Split per-minute net kWh into (imported, exported, last minute counted).

    Only complete minutes after `after` are counted. Counting stops at the
    first minute Emporia has no value for yet, so it is retried next time,
    unless that minute is older than GIVE_UP_AFTER.
    """
    imported = exported = 0.0
    last: datetime | None = None
    current_minute = floor_minute(now)
    for index, value in enumerate(values):
        minute = first_minute + index * MINUTE
        if minute >= current_minute:
            break  # not complete yet
        if after is not None and minute <= after:
            continue
        if value is None:
            if now - minute < GIVE_UP_AFTER:
                break
            value = 0.0
        if value >= 0:
            imported += value
        else:
            exported -= value
        last = minute
    return imported, exported, last


class GridEnergyTracker:
    """Keeps grid import/export totals per monitor, persisted across restarts."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the tracker."""
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.grid_energy.{entry_id}"
        )
        self._totals: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Load saved totals."""
        self._totals = await self._store.async_load() or {}

    async def async_update(
        self, vue: PyEmVue, device_gids: list[int]
    ) -> dict[str, dict[str, Any]]:
        """Count the minutes since the last update for each monitor."""
        loop = asyncio.get_running_loop()
        now = datetime.now(UTC)
        changed = False
        try:
            for gid in device_gids:
                key = str(gid)
                totals = self._totals.get(key)
                if totals is None:
                    # Start counting from now; there is nothing to compare to
                    self._totals[key] = {
                        "import_kwh": 0.0,
                        "export_kwh": 0.0,
                        "last_minute": (floor_minute(now) - MINUTE).isoformat(),
                    }
                    changed = True
                    continue
                last = datetime.fromisoformat(totals["last_minute"])
                start = max(last + MINUTE, now - MAX_BACKFILL)
                if start >= floor_minute(now):
                    continue
                channel = VueDeviceChannel(gid=gid, channelNum=MAINS_CHANNEL)
                values, first_minute = await loop.run_in_executor(
                    None,
                    vue.get_chart_usage,
                    channel,
                    start,
                    now,
                    Scale.MINUTE.value,
                )
                if not values or first_minute is None:
                    continue
                imported, exported, counted_to = split_minutes(
                    values, floor_minute(first_minute), last, now
                )
                if counted_to is None:
                    continue
                totals["import_kwh"] += imported
                totals["export_kwh"] += exported
                totals["last_minute"] = counted_to.isoformat()
                changed = True
        except Exception as err:
            _LOGGER.debug("Error fetching grid energy from Emporia", exc_info=True)
            raise UpdateFailed(f"Error fetching grid energy: {err}") from err
        if changed:
            await self._store.async_save(self._totals)
        return {key: dict(value) for key, value in self._totals.items()}
