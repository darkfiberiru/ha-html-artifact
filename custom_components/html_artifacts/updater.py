"""Self-update from GitHub, with no Home Assistant imports so it can be unit tested.

Flow: look up the newest version of a GitHub repo (the latest release, or the
version in `manifest.json` on the default branch when there are no releases),
download its tarball, and swap `custom_components/<domain>` for the copy inside
it. Works for private repos when given a token (fine-grained, read-only
"Contents" access to that one repo is enough).
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

API = "https://api.github.com"
MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_UNPACKED = 50 * 1024 * 1024
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class UpdateError(Exception):
    """A problem worth showing to the user as-is."""


@dataclass(frozen=True)
class Release:
    version: str
    tarball_url: str
    notes: str
    url: str


def parse_version(text: str) -> tuple[int, ...]:
    match = re.match(r"^v?(\d+(?:\.\d+)*)", str(text).strip())
    if not match:
        raise ValueError(f"not a version: {text!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(candidate: str, installed: str) -> bool:
    try:
        a, b = parse_version(candidate), parse_version(installed)
    except ValueError:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def _headers(token: str | None, accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {"Accept": accept, "User-Agent": "ha-html-artifacts", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _get(session: Any, url: str, token: str | None, *, accept: str | None = None, as_text: bool = False):
    """GET and decode; returns None on 404. `session` is an aiohttp.ClientSession."""
    headers = _headers(token, accept) if accept else _headers(token)
    async with session.get(url, headers=headers) as resp:
        if resp.status == 404:
            return None
        if resp.status in (401, 403):
            hint = "rate limit reached" if resp.headers.get("X-RateLimit-Remaining") == "0" else "check the GitHub token"
            raise UpdateError(f"GitHub refused access (HTTP {resp.status}): {hint}.")
        if resp.status >= 400:
            raise UpdateError(f"GitHub returned HTTP {resp.status}.")
        return await (resp.text() if as_text else resp.json())


async def fetch_latest(session: Any, repo: str, token: str | None, domain: str) -> Release:
    if not REPO_RE.match(repo or ""):
        raise UpdateError(f"'{repo}' is not a GitHub repository in owner/name form.")
    info = await _get(session, f"{API}/repos/{repo}", token)
    if info is None:
        raise UpdateError(
            f"Repository {repo} not found. If it is private, add a GitHub token under Configure."
        )
    branch = info.get("default_branch") or "main"
    release = await _get(session, f"{API}/repos/{repo}/releases/latest", token)
    if release and release.get("tag_name"):
        return Release(
            version=str(release["tag_name"]).lstrip("v"),
            tarball_url=release.get("tarball_url") or f"{API}/repos/{repo}/tarball/{release['tag_name']}",
            notes=release.get("body") or "",
            url=release.get("html_url") or f"https://github.com/{repo}/releases",
        )
    # No releases: follow the version in manifest.json on the default branch.
    text = await _get(
        session,
        f"{API}/repos/{repo}/contents/custom_components/{domain}/manifest.json?ref={branch}",
        token,
        accept="application/vnd.github.raw+json",
        as_text=True,
    )
    if text is None:
        raise UpdateError(f"{repo} has no custom_components/{domain}/manifest.json on {branch}.")
    try:
        version = str(json.loads(text)["version"])
    except (ValueError, KeyError, TypeError) as err:
        raise UpdateError("The manifest.json on GitHub is not readable.") from err
    return Release(
        version=version,
        tarball_url=f"{API}/repos/{repo}/tarball/{branch}",
        notes="",
        url=f"https://github.com/{repo}/commits/{branch}",
    )


async def download(session: Any, url: str, token: str | None) -> bytes:
    async with session.get(url, headers=_headers(token, "application/vnd.github+json"), allow_redirects=True) as resp:
        if resp.status in (401, 403, 404):
            raise UpdateError(f"Could not download the update (HTTP {resp.status}); check the GitHub token.")
        if resp.status >= 400:
            raise UpdateError(f"Could not download the update (HTTP {resp.status}).")
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > MAX_DOWNLOAD:
                raise UpdateError("The download is larger than 20 MB, so it was not installed.")
            chunks.append(chunk)
    return b"".join(chunks)


def install_component(data: bytes, domain: str, dest: Path) -> str:
    """Replace `dest` with custom_components/<domain> from a GitHub tarball.

    Blocking (file IO): run it in an executor. Returns the installed version.
    Nothing is touched until the new copy is fully staged and validated, and the
    old copy is restored if the final swap fails.
    """
    dest = Path(dest)
    if dest.is_symlink() or (dest.exists() and os.path.ismount(dest)):
        raise UpdateError(
            "The integration folder is a symlink or mount (managed with git), so it can't be replaced here. Update it with git pull."
        )
    staging = dest.parent / f"_{domain}_new"
    backup = dest.parent / f"_{domain}_old"
    for leftover in (staging, backup):
        shutil.rmtree(leftover, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        total = 0
        found: set[str] = set()
        try:
            tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        except tarfile.TarError as err:
            raise UpdateError("The downloaded file is not a valid archive.") from err
        with tar:
            for member in tar:
                parts = PurePosixPath(member.name).parts
                if len(parts) < 4 or parts[1] != "custom_components" or parts[2] != domain:
                    continue
                rel = parts[3:]
                if any(p in ("", ".", "..") for p in rel) or PurePosixPath(member.name).is_absolute():
                    raise UpdateError("The archive contains an unsafe path.")
                target = staging.joinpath(*rel)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    total += member.size
                    if total > MAX_UNPACKED:
                        raise UpdateError("The update unpacks to more than 50 MB, so it was not installed.")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = tar.extractfile(member)
                    if source is None:
                        continue
                    with source, open(target, "wb") as out:
                        shutil.copyfileobj(source, out)
                    found.add("/".join(rel))
                else:
                    raise UpdateError("The archive contains a link or special file, so it was not installed.")
        for required in ("manifest.json", "__init__.py"):
            if required not in found:
                raise UpdateError(f"The archive has no custom_components/{domain}/{required}.")
        try:
            manifest = json.loads((staging / "manifest.json").read_text("utf-8"))
        except ValueError as err:
            raise UpdateError("The new manifest.json is not valid.") from err
        if manifest.get("domain") != domain or not manifest.get("version"):
            raise UpdateError("The archive is not this integration.")
        if dest.exists():
            dest.rename(backup)
        try:
            staging.rename(dest)
        except OSError:
            if backup.exists():
                backup.rename(dest)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        return str(manifest["version"])
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def hacs_manages(storage_text: str | None, repo: str) -> bool:
    """True if HACS's own storage lists `repo` as installed (so HACS owns updates)."""
    if not storage_text:
        return False
    try:
        data = json.loads(storage_text).get("data")
    except (ValueError, AttributeError):
        return False
    entries = data.values() if isinstance(data, dict) else (data if isinstance(data, list) else [])
    wanted = repo.strip().lower()
    for item in entries:
        if isinstance(item, dict) and str(item.get("full_name", "")).lower() == wanted and item.get("installed"):
            return True
    return False
