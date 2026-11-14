"""Set up the Apple HealthKit Bridge integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS, CONF_WEBHOOK_ID, DEFAULT_OPTIONS_METRIC
from .webhook import AppleHealthManager


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up via YAML is not supported."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Apple HealthKit Bridge from a config entry."""
    manager = AppleHealthManager(hass, entry.entry_id, entry.options or DEFAULT_OPTIONS_METRIC)
    manager.register(entry.data[CONF_WEBHOOK_ID])

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "manager": manager,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register update listener for options changes
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    # Reload the config entry to apply new options
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    manager: AppleHealthManager = hass.data[DOMAIN][entry.entry_id]["manager"]
    manager.unregister(entry.data[CONF_WEBHOOK_ID])

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        unsub = hass.data[DOMAIN][entry.entry_id].get("manager_unsub")
        if unsub:
            unsub()
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
