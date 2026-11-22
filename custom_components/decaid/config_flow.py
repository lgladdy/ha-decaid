"""Manual host configuration; Decaid does not advertise discovery."""

import asyncio
import re
from ipaddress import ip_address
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DecaidClient, DecaidError
from .const import CONF_MACHINE_ID, DEFAULT_PORT, DOMAIN

_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_HOSTNAME = re.compile(rf"(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*")


def normalize_host(value: str) -> str | None:
    """Return a canonical IP address or hostname, or None if it is neither.

    URLs, ports, paths, and bracketed IPv6 literals are rejected.
    """
    host = value.strip()
    try:
        return str(ip_address(host))
    except ValueError:
        pass
    host = host.rstrip(".").lower()
    return host if _HOSTNAME.fullmatch(host) else None


class DecaidConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up and reconfigure a tablet endpoint."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        return await self._async_form("user", user_input)

    async def async_step_reconfigure(self, user_input=None) -> ConfigFlowResult:
        return await self._async_form("reconfigure", user_input)

    async def _async_validate(self, data: dict[str, Any]) -> dict[str, str]:
        """Check the tablet answers and that a given machine ID exists."""
        client = DecaidClient(async_get_clientsession(self.hass), data[CONF_HOST], data[CONF_PORT])
        state, devices = await asyncio.gather(
            client.get("machine/state"), client.get("devices"), return_exceptions=True
        )
        for result in (state, devices):
            if isinstance(result, BaseException) and not isinstance(result, DecaidError):
                raise result
        # A machine that is off at the wall leaves machine/state failing while
        # the tablet itself is fine, so either endpoint proves reachability.
        if isinstance(state, DecaidError) and isinstance(devices, DecaidError):
            return {"base": "cannot_connect"}
        machine_id = data[CONF_MACHINE_ID]
        if (
            machine_id
            and isinstance(devices, list)
            and not any(
                device.get("type") == "machine" and device.get("id") == machine_id
                for device in devices
            )
        ):
            return {CONF_MACHINE_ID: "unknown_machine"}
        return {}

    async def _async_form(self, step: str, user_input: dict[str, Any] | None):
        errors = {}
        entry = self._get_reconfigure_entry() if step == "reconfigure" else None
        defaults = user_input or (entry.data if entry else {})
        if user_input is not None:
            data = dict(user_input)
            host = normalize_host(data[CONF_HOST])
            if host is None:
                errors[CONF_HOST] = "invalid_host"
            else:
                data[CONF_HOST] = host
                for existing in self._async_current_entries():
                    if (not entry or existing.entry_id != entry.entry_id) and (
                        existing.data[CONF_HOST],
                        existing.data[CONF_PORT],
                    ) == (data[CONF_HOST], data[CONF_PORT]):
                        return self.async_abort(reason="already_configured")
                data[CONF_MACHINE_ID] = data.get(CONF_MACHINE_ID, "").strip()
                errors = await self._async_validate(data)
                if not errors:
                    if entry:
                        return self.async_update_reload_and_abort(entry, data_updates=data)
                    return self.async_create_entry(title=f"Decent ({data[CONF_HOST]})", data=data)
        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
                    vol.Required(CONF_PORT, default=defaults.get(CONF_PORT, DEFAULT_PORT)): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=65535)
                    ),
                    vol.Optional(CONF_MACHINE_ID, default=defaults.get(CONF_MACHINE_ID, "")): str,
                }
            ),
            errors=errors,
        )
