"""Custom integration to integrate FGLair Home Assistant Integration
with Home Assistant.
"""

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_REGION, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from pyfujitsugeneral.client import FGLairApiClient

from .const import (
    CONF_TOKENPATH,
    DEFAULT_TIMEOUT,
    DEFAULT_TOKEN_PATH,
    DOMAIN,
    PLATFORMS,
    SCAN_INTERVAL,
    STARTUP_MESSAGE,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Establish connection with FGLair."""

    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(STARTUP_MESSAGE)

    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    region = entry.data.get(CONF_REGION)
    tokenpath = entry.data.get(CONF_TOKENPATH, DEFAULT_TOKEN_PATH)

    session = async_get_clientsession(hass)
    client = FGLairApiClient(username, password, region, tokenpath, session)

    # Eine gemeinsame Anmeldung fuer Hintergrund-Abruf und Klima-Plattform. Scheitert sie (die
    # FGLair-Cloud antwortet nach einem Neustart oft voruebergehend mit 401), meldet die
    # Integration "noch nicht bereit" - Home Assistant versucht es dann selbst erneut, mit
    # wachsendem Abstand, statt aufzugeben.
    try:
        async with asyncio.timeout(DEFAULT_TIMEOUT):
            if not await client.async_authenticate():
                raise ConfigEntryNotReady("FGLair: Anmeldung fehlgeschlagen")
            devices = await client.async_get_devices_dsn()
    except ConfigEntryNotReady:
        raise
    except Exception as exception:
        raise ConfigEntryNotReady(
            f"FGLair: Cloud nicht erreichbar oder Anmeldung fehlgeschlagen ({exception})"
        ) from exception

    coordinator = FglairDataUpdateCoordinator(hass, client=client)
    coordinator.devices = devices
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload FGLair config."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


class FglairDataUpdateCoordinator(DataUpdateCoordinator[None]):
    """Class to manage fetching data from the API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: FGLairApiClient,
    ) -> None:
        """Initialize."""
        self.client = client

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> None:
        """Fetch data from library FGLairApiClient."""
        try:
            async with asyncio.timeout(DEFAULT_TIMEOUT):
                await self.client.async_get_devices_dsn()
        except Exception as exception:
            _LOGGER.warning("Failed to update coordinator data: %s", exception)
            raise UpdateFailed from exception
