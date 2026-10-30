"""Keep the card's module registered as a Lovelace resource.

`frontend.add_extra_js_url()` is enough for the sidebar panel but weak for the
dashboard card. Home Assistant renders an extra module as a fire-and-forget
dynamic `import()` in the page tail:

    import("/html_artifacts_static/html-artifacts.js?v=X").catch(function (err) {
      console.error("Failed to load extra module ...", err);
    });

Two consequences. It races the dashboard's first render, so a card can be built
before `html-artifact-card` is defined; and the `.catch()` swallows a failed
request with no retry, so one flaky fetch leaves the card dead for the whole
page session. Both are likelier on mobile than on a desktop LAN.

A Lovelace resource is part of the dashboard config instead, so the frontend
loads it as part of bringing the dashboard up. Registering both is deliberate:
the module guards every `customElements.define()` with an `if (!customElements
.get(...))`, and pushes to `window.customCards` inside that same guard, so
loading it twice defines nothing twice and cannot duplicate the card picker
entry.
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

RES_TYPE = "module"


def plan_resource(
    items: list[dict[str, Any]], url: str, prefix: str
) -> tuple[str, str | None]:
    """Decide how to sync our resource. Pure, so it is testable without HA.

    Matching is on the path *before* the "?" so that a version bump updates the
    existing row rather than leaving a second one pointing at a stale `?v=`.
    Returns ("noop" | "create" | "update", item_id or None).
    """
    for item in items:
        existing = str(item.get("url") or "")
        if existing.split("?", 1)[0] != prefix:
            continue
        if existing == url:
            return "noop", item.get("id")
        return "update", item.get("id")
    return "create", None


def _resources(hass: Any) -> Any:
    """The Lovelace resource collection, across the shapes HA has used."""
    data = hass.data.get("lovelace")
    if data is None:
        return None
    # 2024.8+ exposes a LovelaceData dataclass; older cores used a plain dict.
    resources = getattr(data, "resources", None)
    if resources is None and isinstance(data, dict):
        resources = data.get("resources")
    return resources


async def async_sync(hass: Any, url: str, prefix: str) -> None:
    """Create or update our resource so it points at the current version."""
    resources = _resources(hass)
    if resources is None:
        _LOGGER.debug("No Lovelace resource collection; relying on extra_js_url")
        return
    try:
        if not getattr(resources, "loaded", True):
            await resources.async_load()
        action, item_id = plan_resource(list(resources.async_items()), url, prefix)
        if action == "create":
            await resources.async_create_item({"res_type": RES_TYPE, "url": url})
            _LOGGER.debug("Registered Lovelace resource %s", url)
        elif action == "update" and item_id:
            await resources.async_update_item(item_id, {"res_type": RES_TYPE, "url": url})
            _LOGGER.debug("Updated Lovelace resource to %s", url)
    except Exception as err:  # noqa: BLE001 - YAML-mode dashboards raise here
        _LOGGER.debug("Lovelace resource sync skipped: %s", err)


async def async_remove(hass: Any, prefix: str) -> None:
    """Drop our resource so an uninstalled integration leaves no dead URL."""
    resources = _resources(hass)
    if resources is None:
        return
    try:
        if not getattr(resources, "loaded", True):
            await resources.async_load()
        for item in list(resources.async_items()):
            if str(item.get("url") or "").split("?", 1)[0] == prefix:
                await resources.async_delete_item(item["id"])
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Lovelace resource removal skipped: %s", err)
