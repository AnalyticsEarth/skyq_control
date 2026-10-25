"""Config flow for Sky Q Control."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from .aioskyq import DeviceInfo, SkyQBox, SkyQError, SkyQUnsupportedDeviceError, discover
from .const import (
    CONF_KEY_GAP,
    CONF_SCAN_INTERVAL,
    DEFAULT_KEY_GAP,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MANUAL_ENTRY,
)

_LOGGER = logging.getLogger(__name__)

HOST_SCHEMA = vol.Schema({vol.Required(CONF_HOST): str})


class CannotConnect(Exception):
    """The box did not answer."""


class UnsupportedDevice(Exception):
    """Not a Sky Q set-top box (e.g. Sky Glass, IP set-top box)."""


def entry_title(info: DeviceInfo, host: str) -> str:
    return "Sky Q Gateway" if info.is_gateway else f"Sky Q Mini ({host})"


class SkyQConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Sky Q gateway or Mini box."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._info: DeviceInfo | None = None
        self._discovered: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SkyQOptionsFlow:
        return SkyQOptionsFlow()

    async def _async_probe(self, host: str) -> DeviceInfo:
        box = SkyQBox(host, async_get_clientsession(self.hass))
        try:
            return await box.get_device_info()
        except SkyQUnsupportedDeviceError as err:
            raise UnsupportedDevice from err
        except SkyQError as err:
            raise CannotConnect from err
        finally:
            await box.close()

    # ------------------------------------------------------------------ user-initiated

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Offer boxes found on the network, or go straight to manual entry."""
        if user_input is not None:
            if user_input[CONF_HOST] == MANUAL_ENTRY:
                return await self.async_step_manual()
            return await self._async_add_host(user_input[CONF_HOST], "manual")

        configured = {entry.data.get(CONF_HOST) for entry in self._async_current_entries(include_ignore=False)}
        try:
            found = await discover()
        except OSError as err:
            _LOGGER.debug("SSDP discovery failed: %s", err)
            found = []
        self._discovered = {
            box.host: f"Sky Q {'Gateway' if box.is_gateway else 'Mini'} ({box.host})"
            for box in found
            if box.host not in configured
        }
        if not self._discovered:
            return await self.async_step_manual()
        options = [SelectOptionDict(value=host, label=label) for host, label in self._discovered.items()]
        options.append(SelectOptionDict(value=MANUAL_ENTRY, label="Enter an IP address"))
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_HOST): SelectSelector(SelectSelectorConfig(options=options, translation_key="host"))}
            ),
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._async_add_host(user_input[CONF_HOST].strip(), "manual")
        return self.async_show_form(step_id="manual", data_schema=HOST_SCHEMA)

    async def _async_add_host(self, host: str, step_id: str) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        try:
            info = await self._async_probe(host)
        except UnsupportedDevice:
            return self.async_abort(reason="unsupported_device")
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error probing Sky Q box at %s", host)
            errors["base"] = "unknown"
        else:
            if not info.unique_id:
                return self.async_abort(reason="no_unique_id")
            await self.async_set_unique_id(info.unique_id)
            self._abort_if_unique_id_configured(updates={CONF_HOST: host})
            return self.async_create_entry(title=entry_title(info, host), data={CONF_HOST: host})
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(HOST_SCHEMA, {CONF_HOST: host}),
            errors=errors,
        )

    # ------------------------------------------------------------------ discovery

    async def async_step_ssdp(self, discovery_info: SsdpServiceInfo) -> ConfigFlowResult:
        host = urlparse(discovery_info.ssdp_location or "").hostname
        if not host:
            return self.async_abort(reason="cannot_connect")
        self._async_abort_entries_match({CONF_HOST: host})
        try:
            info = await self._async_probe(host)
        except UnsupportedDevice:
            return self.async_abort(reason="unsupported_device")
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")
        if not info.unique_id:
            return self.async_abort(reason="no_unique_id")
        await self.async_set_unique_id(info.unique_id)
        # A box that changed IP (DHCP) is updated in place and reloaded.
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        self._host, self._info = host, info
        self.context["title_placeholders"] = {"name": entry_title(info, host)}
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._host is not None and self._info is not None
        if user_input is not None:
            return self.async_create_entry(title=entry_title(self._info, self._host), data={CONF_HOST: self._host})
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": entry_title(self._info, self._host),
                "model": self._info.hardware_model or "unknown",
                "host": self._host,
            },
        )

    # ------------------------------------------------------------------ reconfigure

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Point an existing entry at a new IP address (same box only)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            try:
                info = await self._async_probe(host)
            except UnsupportedDevice:
                return self.async_abort(reason="unsupported_device")
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(info.unique_id)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(entry, data_updates={CONF_HOST: host})
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(HOST_SCHEMA, user_input or dict(entry.data)),
            errors=errors,
        )


class SkyQOptionsFlow(OptionsFlowWithReload):
    """Tune key timing and polling."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        schema = vol.Schema(
            {
                vol.Required(CONF_KEY_GAP, default=DEFAULT_KEY_GAP): NumberSelector(
                    NumberSelectorConfig(
                        min=0.2, max=2.0, step=0.1, unit_of_measurement="s", mode=NumberSelectorMode.SLIDER
                    )
                ),
                vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): NumberSelector(
                    NumberSelectorConfig(min=2, max=60, step=1, unit_of_measurement="s", mode=NumberSelectorMode.BOX)
                ),
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options)
        )
