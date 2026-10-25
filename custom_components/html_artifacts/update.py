"""Update entity: shows in Settings > Updates and installs from GitHub."""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from aiohttp import ClientError

from homeassistant.components import persistent_notification
from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_AUTO_RESTART,
    CONF_FORCE_SELF_UPDATE,
    CONF_GITHUB_TOKEN,
    CONF_REPO,
    DEFAULT_REPO,
    DOMAIN,
    VERSION,
)
from .updater import Release, UpdateError, download, fetch_latest, hacs_manages, install_component, is_newer

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=6)
RESTART_NOTIFICATION = f"{DOMAIN}_restart"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    repo = (entry.options.get(CONF_REPO) or DEFAULT_REPO).strip()
    if not entry.options.get(CONF_FORCE_SELF_UPDATE, False) and await hass.async_add_executor_job(_hacs_owns, hass, repo):
        # HACS already tracks this repo: a second update entity would only disagree with it.
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id("update", DOMAIN, f"{entry.entry_id}_update")
        if entity_id:
            registry.async_remove(entity_id)
        _LOGGER.info("HACS manages %s; the built-in updater is standing down", repo)
        return
    async_add_entities([HtmlArtifactsUpdate(entry)], True)


def _hacs_owns(hass: HomeAssistant, repo: str) -> bool:
    try:
        text = Path(hass.config.path(".storage", "hacs.repositories")).read_text("utf-8")
    except OSError:
        return False
    return hacs_manages(text, repo)


class HtmlArtifactsUpdate(UpdateEntity):
    _attr_has_entity_name = True
    _attr_name = "Update"
    _attr_title = "HTML Artifacts"
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.RELEASE_NOTES

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_update"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name="HTML Artifacts", entry_type=None
        )
        self._installed = VERSION
        self._release: Release | None = None
        self._error: str | None = None
        self._restart_required = False

    # ---- state -------------------------------------------------------------
    @property
    def installed_version(self) -> str:
        return self._installed

    @property
    def latest_version(self) -> str:
        if self._release and is_newer(self._release.version, self._installed):
            return self._release.version
        return self._installed

    @property
    def release_url(self) -> str | None:
        return self._release.url if self._release else None

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "repository": self._repo,
            "restart_required": self._restart_required,
            "last_error": self._error,
            "authenticated": bool(self._token),
        }

    @property
    def _repo(self) -> str:
        return (self._entry.options.get(CONF_REPO) or DEFAULT_REPO).strip()

    @property
    def _token(self) -> str | None:
        return (self._entry.options.get(CONF_GITHUB_TOKEN) or "").strip() or None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._entry.add_update_listener(self._options_changed))

    async def _options_changed(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        await self.async_update_ha_state(True)

    # ---- polling -----------------------------------------------------------
    async def async_update(self) -> None:
        session = async_get_clientsession(self.hass)
        try:
            self._release = await fetch_latest(session, self._repo, self._token, DOMAIN)
            self._error = None
        except UpdateError as err:
            self._error = str(err)
            _LOGGER.warning("Update check failed: %s", err)
        except (ClientError, TimeoutError) as err:
            self._error = f"Could not reach GitHub: {err}"
            _LOGGER.warning("Update check failed: %s", self._error)

    async def async_release_notes(self) -> str | None:
        return (self._release.notes or None) if self._release else None

    # ---- install -----------------------------------------------------------
    async def async_install(self, version: str | None, backup: bool, **kwargs) -> None:
        release = self._release
        if release is None or not is_newer(release.version, self._installed):
            raise HomeAssistantError("There is no newer version to install. Check for updates first.")
        session = async_get_clientsession(self.hass)
        dest = Path(self.hass.config.path("custom_components", DOMAIN))
        try:
            data = await download(session, release.tarball_url, self._token)
            new_version = await self.hass.async_add_executor_job(install_component, data, DOMAIN, dest)
        except UpdateError as err:
            raise HomeAssistantError(str(err)) from err
        except (ClientError, TimeoutError) as err:
            raise HomeAssistantError(f"Could not reach GitHub: {err}") from err
        _LOGGER.info("Installed html_artifacts %s (was %s); restart required", new_version, self._installed)
        self._installed = new_version
        self._restart_required = True
        self.async_write_ha_state()
        if self._entry.options.get(CONF_AUTO_RESTART, False):
            await self.hass.services.async_call("homeassistant", "restart", blocking=False)
        else:
            persistent_notification.async_create(
                self.hass,
                f"HTML Artifacts {new_version} is installed. Restart Home Assistant to start using it.",
                title="HTML Artifacts updated",
                notification_id=RESTART_NOTIFICATION,
            )
