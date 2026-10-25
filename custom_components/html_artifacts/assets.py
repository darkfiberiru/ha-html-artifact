"""Per-artifact asset files (images, 3D models, data) kept on disk.

A page is limited to MAX_HTML_BYTES and lives in HA's `.storage` with its
history, so anything bulky goes here instead:

    <config>/html_artifacts/artifacts/<artifact_id>/<path>    the files
    <config>/html_artifacts/artifacts/<artifact_id>/.index.json  size + sha256 per file
    <config>/html_artifacts/inbox/                            zips waiting to be imported

Files have no version history (they are not in `.storage`), are served to the
page through the authenticated bridge (never from /local), and are deleted with
their artifact.

Nothing in here imports Home Assistant, so it can be unit tested on its own.
All methods do blocking file I/O: call them from an executor.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .core import SLUG_RE, ArtifactError

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILES = 1000
MAX_PATH = 200
MAX_DEPTH = 8
MAX_ZIP_RATIO = 200  # refuse zip members that inflate more than this (zip bombs)

SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".json": "application/json",
    ".geojson": "application/geo+json",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".pdf": "application/pdf",
    ".stl": "model/stl",
    ".obj": "model/obj",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".3mf": "model/3mf",
    ".bin": "application/octet-stream",
}

UPLOAD_TTL_DEFAULT = 15 * 60
UPLOAD_TTL_MAX = 60 * 60
MAX_TICKETS = 100

_INDEX = ".index.json"
_STAGING = ".staging"


def clean_path(path: Any) -> str:
    """Validate an asset path: relative, '/'-separated, plain names, known extension."""
    if not isinstance(path, str):
        raise ArtifactError("invalid", "asset path must be a string")
    path = path.strip().lstrip("/")
    if not path or len(path) > MAX_PATH:
        raise ArtifactError("invalid", f"asset path must be 1-{MAX_PATH} characters")
    parts = path.split("/")
    if len(parts) > MAX_DEPTH:
        raise ArtifactError("invalid", f"asset path is deeper than {MAX_DEPTH} folders")
    for part in parts:
        if not SEGMENT_RE.match(part) or part in (".", ".."):
            raise ArtifactError(
                "invalid",
                f"bad asset path '{path}': use letters, digits, '.', '_' or '-', not starting with '.'",
            )
    if PurePosixPath(path).suffix.lower() not in CONTENT_TYPES:
        raise ArtifactError(
            "invalid",
            f"'{path}': file type not allowed (allowed: {', '.join(sorted(CONTENT_TYPES))})",
        )
    return path


def content_type(path: str) -> str:
    return CONTENT_TYPES.get(PurePosixPath(path).suffix.lower(), "application/octet-stream")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AssetStore:
    """Asset files for every artifact, under one root folder."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # -- locations ----------------------------------------------------------

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    def _dir(self, artifact_id: str) -> Path:
        if not isinstance(artifact_id, str) or not SLUG_RE.match(artifact_id):
            raise ArtifactError("invalid", "bad artifact id")
        return self.root / "artifacts" / artifact_id

    def _file(self, artifact_id: str, path: str) -> Path:
        return self._dir(artifact_id) / clean_path(path)

    # -- index ----------------------------------------------------------------

    def _load_index(self, artifact_id: str) -> dict[str, dict]:
        base = self._dir(artifact_id)
        index_file = base / _INDEX
        if index_file.is_file():
            try:
                data = json.loads(index_file.read_text("utf-8"))
                if isinstance(data, dict):
                    return data
            except (OSError, ValueError):
                pass
        return self._rebuild_index(artifact_id)

    def _rebuild_index(self, artifact_id: str) -> dict[str, dict]:
        base = self._dir(artifact_id)
        index: dict[str, dict] = {}
        if base.is_dir():
            for file in sorted(base.rglob("*")):
                rel = file.relative_to(base).as_posix()
                if not file.is_file() or file.is_symlink() or rel.startswith("."):
                    continue
                try:
                    rel = clean_path(rel)
                except ArtifactError:
                    continue
                data = file.read_bytes()
                index[rel] = {"size": len(data), "sha256": _sha256(data)}
        self._write_index(artifact_id, index)
        return index

    def _write_index(self, artifact_id: str, index: dict[str, dict]) -> None:
        base = self._dir(artifact_id)
        if not index and not base.exists():
            return
        base.mkdir(parents=True, exist_ok=True)
        tmp = base / f"{_INDEX}.{uuid.uuid4().hex}"
        tmp.write_text(json.dumps(index, sort_keys=True), "utf-8")
        os.replace(tmp, base / _INDEX)

    @staticmethod
    def _entry(path: str, meta: dict) -> dict[str, Any]:
        return {"path": path, "size": meta["size"], "sha256": meta["sha256"], "content_type": content_type(path)}

    # -- reading ----------------------------------------------------------------

    def list(self, artifact_id: str) -> dict[str, Any]:
        index = self._load_index(artifact_id)
        files = [self._entry(p, m) for p, m in sorted(index.items())]
        return {
            "files": files,
            "count": len(files),
            "total_bytes": sum(f["size"] for f in files),
            "limits": {"file_bytes": MAX_FILE_BYTES, "total_bytes": MAX_TOTAL_BYTES, "files": MAX_FILES},
        }

    def read(self, artifact_id: str, path: str) -> tuple[bytes, dict[str, Any]]:
        path = clean_path(path)
        meta = self._load_index(artifact_id).get(path)
        file = self._file(artifact_id, path)
        if meta is None or not file.is_file() or file.is_symlink():
            raise ArtifactError("not_found", f"asset '{path}' does not exist")
        return file.read_bytes(), self._entry(path, meta)

    # -- writing ----------------------------------------------------------------

    def _check_room(self, index: dict[str, dict], path: str, size: int, adding: int = 1) -> None:
        if size > MAX_FILE_BYTES:
            raise ArtifactError("too_large", f"'{path}' is larger than {MAX_FILE_BYTES} bytes")
        others = sum(m["size"] for p, m in index.items() if p != path)
        if others + size > MAX_TOTAL_BYTES:
            raise ArtifactError("too_large", f"assets would exceed {MAX_TOTAL_BYTES} bytes for this artifact")
        if path not in index and len(index) + adding > MAX_FILES:
            raise ArtifactError("limit", f"at most {MAX_FILES} asset files per artifact")

    def _place(self, artifact_id: str, path: str, source: Path) -> None:
        target = self._file(artifact_id, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)

    def put(
        self,
        artifact_id: str,
        path: str,
        data: bytes,
        *,
        offset: int = 0,
        final: bool = True,
        sha256: str | None = None,
    ) -> dict[str, Any]:
        """Write a file, whole or in chunks.

        Chunks go to a staging file: the first has offset 0, each next one the
        size received so far, and the last one `final=True`, which moves the
        file into place (checking `sha256` if given). Returns the file entry, or
        `{path, received, final: False}` for a non-final chunk.
        """
        path = clean_path(path)
        if not isinstance(data, (bytes, bytearray)):
            raise ArtifactError("invalid", "data must be bytes")
        if not isinstance(offset, int) or offset < 0:
            raise ArtifactError("invalid", "offset must be a non-negative integer")
        base = self._dir(artifact_id)
        staging = base / _STAGING
        staging.mkdir(parents=True, exist_ok=True)
        part = staging / (hashlib.sha1(path.encode()).hexdigest() + ".part")

        have = part.stat().st_size if part.exists() else 0
        if offset == 0:
            have = 0
        elif offset != have:
            raise ArtifactError(
                "conflict", f"'{path}': expected offset {have}, got {offset} (resend from {have})", received=have
            )
        if have + len(data) > MAX_FILE_BYTES:
            part.unlink(missing_ok=True)
            raise ArtifactError("too_large", f"'{path}' is larger than {MAX_FILE_BYTES} bytes")
        with open(part, "r+b" if offset else "wb") as fh:
            fh.seek(offset)
            fh.write(data)
            fh.truncate()
        if not final:
            return {"path": path, "received": have + len(data), "final": False}

        blob = part.read_bytes()
        digest = _sha256(blob)
        if sha256 and sha256.lower() != digest:
            part.unlink(missing_ok=True)
            raise ArtifactError("invalid", f"'{path}': sha256 mismatch (got {digest}); upload it again")
        index = self._load_index(artifact_id)
        try:
            self._check_room(index, path, len(blob))
        except ArtifactError:
            part.unlink(missing_ok=True)
            raise
        self._place(artifact_id, path, part)
        index[path] = {"size": len(blob), "sha256": digest}
        self._write_index(artifact_id, index)
        return {**self._entry(path, index[path]), "final": True}

    def delete(self, artifact_id: str, path: str | None = None) -> int:
        """Delete one file, or every asset of the artifact when `path` is None."""
        base = self._dir(artifact_id)
        if path is None:
            count = len(self._load_index(artifact_id)) if base.exists() else 0
            shutil.rmtree(base, ignore_errors=True)
            return count
        path = clean_path(path)
        index = self._load_index(artifact_id)
        if path not in index:
            raise ArtifactError("not_found", f"asset '{path}' does not exist")
        self._file(artifact_id, path).unlink(missing_ok=True)
        del index[path]
        # prune empty folders up to the artifact folder
        parent = self._file(artifact_id, path).parent
        while parent != base and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
        self._write_index(artifact_id, index)
        return 1

    # -- zip import ----------------------------------------------------------------

    def inbox_file(self, name: str) -> Path:
        """Resolve a file name inside the inbox (no links, no escaping)."""
        if not isinstance(name, str) or not name.strip():
            raise ArtifactError("invalid", "inbox file name must not be empty")
        rel = name.strip().lstrip("/")
        if rel.startswith("inbox/"):
            rel = rel[len("inbox/"):]
        parts = rel.split("/")
        if any(not SEGMENT_RE.match(p) for p in parts) or not rel.lower().endswith(".zip"):
            raise ArtifactError("invalid", f"'{name}' must be a .zip file name inside the inbox")
        file = self.inbox.joinpath(*parts)
        try:
            st = os.lstat(file)
        except FileNotFoundError:
            raise ArtifactError("not_found", f"{file} does not exist") from None
        if not stat.S_ISREG(st.st_mode):
            raise ArtifactError("invalid", f"{file} is not a regular file")
        return file

    def import_zip(
        self,
        artifact_id: str,
        source: bytes | Path,
        *,
        prefix: str = "",
        replace: bool = False,
    ) -> dict[str, Any]:
        """Unpack a zip into the artifact's assets.

        Every member is checked before anything is written: plain relative
        paths, allowed file types, no links, size and count limits, and no
        absurd compression ratios. `prefix` puts everything under a folder;
        `replace` removes the existing assets first. Directories are skipped.
        """
        prefix = prefix.strip().strip("/") if prefix else ""
        if prefix:
            for part in prefix.split("/"):
                if not SEGMENT_RE.match(part):
                    raise ArtifactError("invalid", f"bad prefix '{prefix}'")
        raw = source if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
        if len(raw) > MAX_TOTAL_BYTES * 2:
            raise ArtifactError("too_large", "zip is too large")
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as err:
            raise ArtifactError("invalid", f"not a zip file: {err}") from err

        members: list[tuple[zipfile.ZipInfo, str]] = []
        with archive:
            for info in archive.infolist():
                if info.is_dir() or info.filename.startswith("__MACOSX/"):
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode and mode != stat.S_IFREG:
                    raise ArtifactError("invalid", f"zip member '{info.filename}' is not a regular file")
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    raise ArtifactError("invalid", f"zip member '{info.filename}' has an unsafe path")
                path = clean_path(f"{prefix}/{name}" if prefix else name)
                if info.file_size > MAX_FILE_BYTES:
                    raise ArtifactError("too_large", f"'{path}' is larger than {MAX_FILE_BYTES} bytes")
                if info.compress_size and info.file_size / info.compress_size > MAX_ZIP_RATIO:
                    raise ArtifactError("invalid", f"zip member '{info.filename}' has a suspicious compression ratio")
                members.append((info, path))

            index = self._load_index(artifact_id)
            incoming = {path: info.file_size for info, path in members}
            if len(incoming) != len(members):
                raise ArtifactError("invalid", "zip has duplicate paths")
            kept = {} if replace else {p: m for p, m in index.items() if p not in incoming}
            if sum(m["size"] for m in kept.values()) + sum(incoming.values()) > MAX_TOTAL_BYTES:
                raise ArtifactError("too_large", f"assets would exceed {MAX_TOTAL_BYTES} bytes for this artifact")
            if len(kept) + len(incoming) > MAX_FILES:
                raise ArtifactError("limit", f"at most {MAX_FILES} asset files per artifact")

            base = self._dir(artifact_id)
            staging = base / _STAGING / f"zip-{uuid.uuid4().hex}"
            staging.mkdir(parents=True, exist_ok=True)
            try:
                written: dict[str, dict] = {}
                for info, path in members:
                    with archive.open(info) as fh:
                        data = fh.read(MAX_FILE_BYTES + 1)
                    if len(data) > MAX_FILE_BYTES or len(data) != info.file_size:
                        raise ArtifactError("invalid", f"zip member '{info.filename}' is corrupt or too large")
                    tmp = staging / uuid.uuid4().hex
                    tmp.write_bytes(data)
                    written[path] = {"size": len(data), "sha256": _sha256(data), "tmp": tmp}
                if replace:
                    for old in index:
                        if old not in written:
                            self._file(artifact_id, old).unlink(missing_ok=True)
                for path, meta in written.items():
                    self._place(artifact_id, path, meta.pop("tmp"))
                    kept[path] = meta
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        self._write_index(artifact_id, kept)
        return {
            "imported": len(written),
            "bytes": sum(m["size"] for m in written.values()),
            "files": [self._entry(p, kept[p]) for p in sorted(written)],
            "total_bytes": sum(m["size"] for m in kept.values()),
            "count": len(kept),
        }


class UploadTickets:
    """Single-use, short-lived upload URLs (the "signed URL" pattern).

    An MCP tool issues a ticket bound to one artifact and one target (a file
    path, or a zip to import); the caller then PUTs the bytes straight to
    /api/html_artifacts/upload/<token> (e.g. with curl), so a large file never
    passes through the model's context. Tickets live in memory only.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._tickets: dict[str, dict[str, Any]] = {}

    def _prune(self) -> None:
        now = self._clock()
        for token in [t for t, v in self._tickets.items() if v["expires_at"] <= now]:
            del self._tickets[token]

    def issue(
        self,
        artifact_id: str,
        *,
        mode: str,
        path: str | None = None,
        prefix: str = "",
        replace: bool = False,
        sha256: str | None = None,
        ttl: float = UPLOAD_TTL_DEFAULT,
    ) -> dict[str, Any]:
        if not isinstance(artifact_id, str) or not SLUG_RE.match(artifact_id):
            raise ArtifactError("invalid", "bad artifact id")
        if mode == "file":
            path = clean_path(path)
            max_bytes = MAX_FILE_BYTES
        elif mode == "zip":
            path = None
            max_bytes = MAX_TOTAL_BYTES * 2
        else:
            raise ArtifactError("invalid", "mode must be 'file' or 'zip'")
        if sha256 is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
            raise ArtifactError("invalid", "sha256 must be 64 hex digits")
        ttl = max(60.0, min(float(ttl), UPLOAD_TTL_MAX))
        self._prune()
        if len(self._tickets) >= MAX_TICKETS:
            raise ArtifactError("limit", f"at most {MAX_TICKETS} open upload URLs")
        token = secrets.token_urlsafe(32)
        ticket = {
            "artifact_id": artifact_id,
            "mode": mode,
            "path": path,
            "prefix": prefix or "",
            "replace": bool(replace),
            "sha256": sha256.lower() if sha256 else None,
            "max_bytes": max_bytes,
            "expires_at": self._clock() + ttl,
        }
        self._tickets[token] = ticket
        return {"token": token, **ticket}

    def take(self, token: str) -> dict[str, Any]:
        """Use up a ticket. Unknown, used and expired tickets all look the same."""
        self._prune()
        ticket = self._tickets.pop(token, None) if isinstance(token, str) else None
        if ticket is None:
            raise ArtifactError("forbidden", "upload URL is unknown, used or expired: ask for a new one")
        return ticket
