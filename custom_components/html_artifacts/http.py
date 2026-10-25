"""REST API: lets Claude (or any script) publish artifacts with a long-lived token.

  GET    /api/html_artifacts              list
  GET    /api/html_artifacts/<id>         read (includes html and version)
  PUT    /api/html_artifacts/<id>         publish {html, base_version?, title?, description?, icon?, capabilities?}
  DELETE /api/html_artifacts/<id>         delete (admin)

An update must carry the `base_version` that was read; a stale one gets a 409
with the current content so the caller can merge hand edits before retrying.
"""
from __future__ import annotations

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .core import ArtifactError

_STATUS = {
    "not_found": 404,
    "conflict": 409,
    "too_large": 413,
    "forbidden": 403,
    "invalid": 400,
    "limit": 400,
}


class _Base(HomeAssistantView):
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _runtime(self):
        rt = self.hass.data.get(DOMAIN, {}).get("runtime")
        if rt is None:
            raise ArtifactError("not_found", "html_artifacts is not set up")
        return rt

    def _fail(self, err: ArtifactError) -> web.Response:
        return self.json(
            {"error": err.code, "message": err.message, **err.extra},
            status_code=_STATUS.get(err.code, 400),
        )


class ArtifactListView(_Base):
    url = "/api/html_artifacts"
    name = "api:html_artifacts:list"

    async def get(self, request: web.Request) -> web.Response:
        try:
            return self.json({"artifacts": self._runtime().artifacts.list()})
        except ArtifactError as err:
            return self._fail(err)


class ArtifactView(_Base):
    url = "/api/html_artifacts/{artifact_id}"
    name = "api:html_artifacts:item"

    async def get(self, request: web.Request, artifact_id: str) -> web.Response:
        try:
            return self.json(self._runtime().artifacts.get(artifact_id))
        except ArtifactError as err:
            return self._fail(err)

    async def put(self, request: web.Request, artifact_id: str) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            return self.json({"error": "invalid", "message": "body must be JSON"}, status_code=400)
        if not isinstance(body, dict) or "html" not in body:
            return self.json({"error": "invalid", "message": "body needs an 'html' field"}, status_code=400)
        user = request.get("hass_user")
        try:
            result = self._runtime().artifacts.save(
                artifact_id,
                body["html"],
                title=body.get("title"),
                description=body.get("description"),
                icon=body.get("icon"),
                base_version=body.get("base_version"),
                caps_requested=body.get("capabilities"),
                source="api",
                updated_by=f"api:{user.name}" if user else "api",
            )
        except ArtifactError as err:
            return self._fail(err)
        return self.json(result)

    async def delete(self, request: web.Request, artifact_id: str) -> web.Response:
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            return self.json({"error": "forbidden", "message": "admin only"}, status_code=403)
        try:
            self._runtime().delete_artifact(artifact_id)
        except ArtifactError as err:
            return self._fail(err)
        return self.json({})


def async_register_http(hass: HomeAssistant) -> None:
    hass.http.register_view(ArtifactListView(hass))
    hass.http.register_view(ArtifactView(hass))
