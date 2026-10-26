"""REST API: lets Claude (or any script) publish artifacts with a long-lived token.

  GET    /api/html_artifacts              list
  GET    /api/html_artifacts/<id>         read (includes html and version)
  GET    /api/html_artifacts/<id>?metadata=1   version, size, capabilities only (no html)
  PUT    /api/html_artifacts/<id>         publish {html, base_version?, title?, description?, icon?, display?, card_height?, capabilities?}
  PATCH  /api/html_artifacts/<id>         {base_version, edits: [{old_str, new_str}], title?, ...}: find/replace edits
  DELETE /api/html_artifacts/<id>         delete (admin)

  GET    /api/html_artifacts/<id>/assets           list asset files
  GET    /api/html_artifacts/<id>/assets/<path>    file bytes (the page gets them through ha.assets)
  PUT    /api/html_artifacts/<id>/assets/<path>    upload a file (raw body; ?sha256= optional)
  DELETE /api/html_artifacts/<id>/assets/<path>    delete a file
  PUT    /api/html_artifacts/upload/<token>        no auth header: a single-use URL from the
                                                   artifact_assets_upload_url tool (file or zip)

An update must carry the `base_version` that was read; a stale one gets a 409
with the current content so the caller can merge hand edits before retrying.
"""
from __future__ import annotations

import hashlib

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .assets import MAX_FILE_BYTES
from .core import ArtifactError

_STATUS = {
    "not_found": 404,
    "conflict": 409,
    "too_large": 413,
    "forbidden": 403,
    "invalid": 400,
    "edit_failed": 422,
    "limit": 400,
}


async def read_body(request: web.Request, limit: int) -> bytes:
    """Read the whole request body as it streams in, refusing more than `limit` bytes.

    (StreamReader.read(n) returns as soon as *some* data is there, so one call is
    not enough for a large upload; request.read() would hit HA's 16 MB cap.)
    """
    if request.content_length and request.content_length > limit:
        raise ArtifactError("too_large", f"upload is larger than {limit} bytes")
    chunks, size = [], 0
    while True:
        chunk = await request.content.read(1 << 16)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise ArtifactError("too_large", f"upload is larger than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


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
            artifact = self._runtime().artifacts.get(artifact_id)
        except ArtifactError as err:
            return self._fail(err)
        if request.query.get("metadata") in ("1", "true"):
            artifact.pop("html", None)
        return self.json(artifact)

    async def patch(self, request: web.Request, artifact_id: str) -> web.Response:
        try:
            body = await request.json()
        except ValueError:
            return self.json({"error": "invalid", "message": "body must be JSON"}, status_code=400)
        if not isinstance(body, dict) or "edits" not in body:
            return self.json({"error": "invalid", "message": "body needs an 'edits' field"}, status_code=400)
        user = request.get("hass_user")
        try:
            result = self._runtime().artifacts.patch(
                artifact_id,
                body["edits"],
                base_version=body.get("base_version"),
                title=body.get("title"),
                description=body.get("description"),
                icon=body.get("icon"),
                display=body.get("display"),
                card_height=body.get("card_height"),
                caps_requested=body.get("capabilities"),
                source="api",
                updated_by=f"api:{user.name}" if user else "api",
            )
        except ArtifactError as err:
            return self._fail(err)
        result.pop("html", None)
        return self.json(result)

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
                display=body.get("display"),
                card_height=body.get("card_height"),
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


class AssetListView(_Base):
    url = "/api/html_artifacts/{artifact_id}/assets"
    name = "api:html_artifacts:assets"

    async def get(self, request: web.Request, artifact_id: str) -> web.Response:
        try:
            rt = self._runtime()
            if not rt.artifacts.exists(artifact_id):
                raise ArtifactError("not_found", f"artifact '{artifact_id}' does not exist")
            return self.json(await rt.async_assets(rt.assets.list, artifact_id))
        except ArtifactError as err:
            return self._fail(err)


class AssetView(_Base):
    url = "/api/html_artifacts/{artifact_id}/assets/{path:.+}"
    name = "api:html_artifacts:asset"

    def _existing(self, artifact_id: str):
        rt = self._runtime()
        if not rt.artifacts.exists(artifact_id):
            raise ArtifactError("not_found", f"artifact '{artifact_id}' does not exist")
        return rt

    async def get(self, request: web.Request, artifact_id: str, path: str) -> web.Response:
        try:
            rt = self._existing(artifact_id)
            data, entry = await rt.async_assets(rt.assets.read, artifact_id, path)
        except ArtifactError as err:
            return self._fail(err)
        etag = f'"{entry["sha256"]}"'
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})
        return web.Response(
            body=data,
            content_type=entry["content_type"].split(";")[0],
            headers={
                "ETag": etag,
                "Cache-Control": "private, no-cache",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
            },
        )

    async def put(self, request: web.Request, artifact_id: str, path: str) -> web.Response:
        try:
            rt = self._existing(artifact_id)
            data = await read_body(request, MAX_FILE_BYTES)
            entry = await rt.async_assets(
                rt.assets.put, artifact_id, path, data, sha256=request.query.get("sha256")
            )
        except ArtifactError as err:
            return self._fail(err)
        return self.json(entry)

    async def delete(self, request: web.Request, artifact_id: str, path: str) -> web.Response:
        try:
            rt = self._existing(artifact_id)
            await rt.async_assets(rt.assets.delete, artifact_id, path)
        except ArtifactError as err:
            return self._fail(err)
        return self.json({})


class UploadView(_Base):
    """Signed-URL upload: the token is the credential (single use, expires)."""

    url = "/api/html_artifacts/upload/{token}"
    name = "api:html_artifacts:upload"
    requires_auth = False

    async def put(self, request: web.Request, token: str) -> web.Response:
        try:
            rt = self._runtime()
            ticket = rt.uploads.take(token)
            aid = ticket["artifact_id"]
            if not rt.artifacts.exists(aid):
                raise ArtifactError("not_found", f"artifact '{aid}' does not exist")
            data = await read_body(request, ticket["max_bytes"])
            if ticket["mode"] == "file":
                result = await rt.async_assets(rt.assets.put, aid, ticket["path"], data, sha256=ticket["sha256"])
            else:
                if ticket["sha256"] and hashlib.sha256(data).hexdigest() != ticket["sha256"]:
                    raise ArtifactError("invalid", "zip sha256 mismatch: ask for a new URL and upload again")
                result = await rt.async_assets(
                    rt.assets.import_zip, aid, data, prefix=ticket["prefix"], replace=ticket["replace"]
                )
        except ArtifactError as err:
            return self._fail(err)
        return self.json(result)

    post = put


def async_register_http(hass: HomeAssistant) -> None:
    hass.http.register_view(ArtifactListView(hass))
    hass.http.register_view(ArtifactView(hass))
    hass.http.register_view(AssetListView(hass))
    hass.http.register_view(AssetView(hass))
    hass.http.register_view(UploadView(hass))
