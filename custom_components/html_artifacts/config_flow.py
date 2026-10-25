"""Config and options flow."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_AUTO_RESTART,
    CONF_FORCE_SELF_UPDATE,
    CONF_GITHUB_TOKEN,
    CONF_LIVE,
    CONF_NOTIFY_TARGETS,
    CONF_REPO,
    DEFAULT_REPO,
    DOMAIN,
)

_IGNORED = {"send_message", "persistent_notification"}


def _notify_services(hass: HomeAssistant) -> list[str]:
    if hasattr(hass.services, "async_services_for_domain"):
        services = hass.services.async_services_for_domain("notify")
    else:
        services = hass.services.async_services().get("notify", {})
    return sorted(name for name in services if name not in _IGNORED)


class HtmlArtifactsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="HTML Artifacts", data={})
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return HtmlArtifactsOptionsFlow()


class HtmlArtifactsOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_NOTIFY_TARGETS, default=options.get(CONF_NOTIFY_TARGETS, [])
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=_notify_services(self.hass),
                        multiple=True,
                        custom_value=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_LIVE, default=options.get(CONF_LIVE, True)): bool,
                vol.Optional(CONF_REPO, default=options.get(CONF_REPO, DEFAULT_REPO)): str,
                vol.Optional(
                    CONF_GITHUB_TOKEN, description={"suggested_value": options.get(CONF_GITHUB_TOKEN, "")}
                ): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
                vol.Optional(CONF_AUTO_RESTART, default=options.get(CONF_AUTO_RESTART, False)): bool,
                vol.Optional(CONF_FORCE_SELF_UPDATE, default=options.get(CONF_FORCE_SELF_UPDATE, False)): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
