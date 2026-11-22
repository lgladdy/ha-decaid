"""Decaid integration for Decent Espresso machines."""

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later

from .api import DecaidClient
from .const import CONF_MACHINE_ID, DOMAIN, PLATFORMS
from .coordinator import (
    DecaidCoordinator,
    DecaidData,
    DecaidLastShotCoordinator,
    DecaidPushCoordinator,
    DecaidScaleCoordinator,
    DecaidShotCoordinator,
    DecaidShotSettingsCoordinator,
    DecaidWaterCoordinator,
)

type DecaidConfigEntry = ConfigEntry[DecaidData]


async def async_setup_entry(hass: HomeAssistant, entry: DecaidConfigEntry) -> bool:
    """Set up polling and platforms."""
    client = DecaidClient(
        async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT]
    )
    data = DecaidData(
        client,
        DecaidPushCoordinator(hass, client, "machine/state", 10, "machine/snapshot"),
        DecaidCoordinator(hass, client, "workflow", 60),
        DecaidCoordinator(hass, client, "settings", 60),
        DecaidPushCoordinator(hass, client, "devices", 60, "devices"),
        DecaidWaterCoordinator(hass, client),
        DecaidScaleCoordinator(hass, client),
        DecaidShotCoordinator(hass, client),
        DecaidShotSettingsCoordinator(hass, client),
        # History totals: limit=1 keeps the payload small, `total` is the full count.
        DecaidLastShotCoordinator(hass, client),
        DecaidCoordinator(hass, client, "steams/ids", 60),
        DecaidCoordinator(hass, client, "machine/info", 300),
        DecaidCoordinator(hass, client, "machine/capabilities", 300),
        # Each read is several BLE register reads; writes refresh it at once.
        DecaidCoordinator(hass, client, "machine/settings", 300),
        DecaidCoordinator(hass, client, "display", 60),
        DecaidCoordinator(hass, client, "presence/settings", 60),
        DecaidCoordinator(hass, client, "update", 900),
        DecaidCoordinator(hass, client, "machine/firmware", 3600),
        # Bengle only: these poll only once their entities have been added.
        DecaidCoordinator(hass, client, "machine/cupWarmer", 30),
        DecaidCoordinator(hass, client, "machine/cupWarmer/preheat", 60),
    )
    # The tablet is reachable if either the machine state or the device
    # inventory answers. A machine that is off at the wall leaves machine/state
    # failing, but the tablet's own entities should still load.
    results = await asyncio.gather(
        data.machine.async_config_entry_first_refresh(),
        data.devices.async_config_entry_first_refresh(),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException) and not isinstance(result, ConfigEntryNotReady):
            raise result
    if all(isinstance(result, ConfigEntryNotReady) for result in results):
        raise results[0]
    # Optional resources can recover after setup instead of blocking all entities.
    await asyncio.gather(
        *(
            c.async_refresh()
            for c in (
                data.workflow,
                data.settings,
                data.shots,
                data.steams,
                data.machine_info,
                data.capabilities,
                data.machine_settings,
                data.display,
                data.presence,
                data.app_update,
                data.firmware,
            )
        )
    )
    entry.runtime_data = data
    machine_was_connected = None

    @callback
    def devices_updated():
        nonlocal machine_was_connected
        if data.devices.last_update_success:
            connected = data.machine_connected(entry.data.get(CONF_MACHINE_ID))
            data.machine.set_device_connected(connected)
            data.water.set_device_connected(connected)
            data.shot_settings.set_device_connected(connected)
            if connected and machine_was_connected is False:
                # Machine-backed resources fail while it is away; reread them
                # now rather than waiting out their long poll intervals.
                for coordinator in (
                    data.machine_info,
                    data.capabilities,
                    data.machine_settings,
                    data.firmware,
                ):
                    entry.async_create_background_task(
                        hass, coordinator.async_request_refresh(), "Decaid machine reconnect"
                    )
            machine_was_connected = connected

    entry.async_on_unload(data.devices.async_add_listener(devices_updated))
    devices_updated()

    @callback
    def machine_info_updated():
        update_device_info(hass, entry, data.machine_info.data)

    entry.async_on_unload(data.machine_info.async_add_listener(machine_info_updated))

    cancel_shot_refresh = None

    @callback
    def refresh_last_shot(_now):
        nonlocal cancel_shot_refresh
        cancel_shot_refresh = None
        entry.async_create_background_task(
            hass, data.shots.async_request_refresh(), "Decaid last shot"
        )

    @callback
    def shot_updated():
        nonlocal cancel_shot_refresh
        shot = data.shot.data or {}
        if shot.get("event") == "terminal" and not data.shot.is_replay:
            # Decaid saves the shot as it finishes; give the write a moment.
            if cancel_shot_refresh is not None:
                cancel_shot_refresh()
            cancel_shot_refresh = async_call_later(hass, 5, refresh_last_shot)

    @callback
    def cancel_pending_shot_refresh():
        if cancel_shot_refresh is not None:
            cancel_shot_refresh()

    entry.async_on_unload(data.shot.async_add_listener(shot_updated))
    entry.async_on_unload(cancel_pending_shot_refresh)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    update_device_info(hass, entry, data.machine_info.data)
    for coordinator in data.streams:
        coordinator.start(entry)
    return True


def _known(value):
    """Decaid reports placeholders before the machine's handshake completes."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    return value if isinstance(value, str) and value not in ("", "0", "Unknown") else None


@callback
def update_device_info(hass: HomeAssistant, entry: DecaidConfigEntry, info) -> None:
    """Show the connected machine's model, serial number, and firmware."""
    registry = dr.async_get(hass)
    identifier = (DOMAIN, entry.entry_id)
    if hasattr(registry, "async_get_device_by_identifier"):
        device = registry.async_get_device_by_identifier(identifier, entry.entry_id)
    else:  # Home Assistant before 2026.11
        device = registry.async_get_device(identifiers={identifier})
    if device is None:
        return
    if not isinstance(info, dict):
        if device.model is None:
            registry.async_update_device(device.id, model="Decaid")
        return
    model = _known(info.get("model")) or (None if device.model else "Decaid")
    changes = {
        key: value
        for key, value in (
            ("model", model),
            ("serial_number", _known(info.get("serialNumber"))),
            ("sw_version", _known(info.get("version"))),
        )
        if value is not None and getattr(device, key) != value
    }
    if changes:
        registry.async_update_device(device.id, **changes)


async def async_unload_entry(hass: HomeAssistant, entry: DecaidConfigEntry) -> bool:
    """Unload platforms and their coordinator listeners."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await asyncio.gather(*(coordinator.stop() for coordinator in entry.runtime_data.streams))
    return True
