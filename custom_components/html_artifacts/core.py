"""Pure-Python core for html_artifacts.

Holds artifact storage (versioned, with history), per-artifact key/value state
and the server-side timer engine. Nothing in here imports Home Assistant, so it
can be unit tested on its own: storage, clock and scheduler are injected.
"""
from __future__ import annotations

import copy
import fnmatch
import json
import logging
import re
import time
import uuid
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

MAX_HTML_BYTES = 2_000_000
MAX_KV_VALUE_BYTES = 256_000
MAX_KV_KEYS = 500
MAX_HISTORY = 20
MAX_TIMERS = 50
MAX_STEPS = 100
MAX_SECONDS = 7 * 86400
FINISHED_RETENTION = 7 * 86400
EPS = 0.25  # tolerance when comparing elapsed time to a step's offset
LATE_AFTER = 5.0  # a step this far behind the clock counts as "missed"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ArtifactError(Exception):
    """Error with a machine-readable code, surfaced by every API front end."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


# --------------------------------------------------------------------------
# Grants
# --------------------------------------------------------------------------

def empty_grants() -> dict[str, Any]:
    return {"entities": [], "services": [], "notify": False, "network": False}


def normalize_grants(raw: Any) -> dict[str, Any]:
    """Validate and normalise a grants/capabilities dict."""
    if raw is None:
        return empty_grants()
    if not isinstance(raw, dict):
        raise ArtifactError("invalid", "capabilities must be an object")

    def strings(key: str) -> list[str]:
        values = raw.get(key) or []
        if not isinstance(values, list) or len(values) > 200:
            raise ArtifactError("invalid", f"{key} must be a list of at most 200 strings")
        out: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip() or len(value) > 128:
                raise ArtifactError("invalid", f"bad entry in {key}")
            value = value.strip().lower()
            if value not in out:
                out.append(value)
        return out

    return {
        "entities": strings("entities"),
        "services": strings("services"),
        "notify": bool(raw.get("notify", False)),
        "network": bool(raw.get("network", False)),
    }


def pattern_match(patterns: list[str], value: str) -> bool:
    value = value.lower()
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


def entity_allowed(grants: dict[str, Any], entity_id: str) -> bool:
    return pattern_match(grants.get("entities", []), entity_id)


def service_allowed(grants: dict[str, Any], domain: str, service: str) -> bool:
    return pattern_match(grants.get("services", []), f"{domain}.{service}")


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

class _Listeners:
    def __init__(self) -> None:
        self._listeners: list[Callable[[dict], None]] = []

    def add_listener(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return remove

    def _emit(self, event: dict) -> None:
        for callback in list(self._listeners):
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - a bad listener must not break the store
                _LOGGER.exception("html_artifacts listener failed")


DISPLAYS = ("card", "page", "both")


def _clean_display(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if value not in DISPLAYS:
        raise ArtifactError("invalid", 'display must be "card", "page" or "both"')
    return value


def _clean_card_height(value: Any) -> int | str | None:
    """Starting height of a dashboard card: pixels (100-4000) or "auto"; None = not given."""
    if value is None or value == "":
        return None
    if value == "auto":
        return "auto"
    if isinstance(value, bool):
        raise ArtifactError("invalid", 'card_height must be a number of pixels or "auto"')
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            raise ArtifactError("invalid", 'card_height must be a number of pixels or "auto"')
    elif not isinstance(value, (int, float)):
        raise ArtifactError("invalid", 'card_height must be a number of pixels or "auto"')
    number = int(float(value))
    if not 100 <= number <= 4000:
        raise ArtifactError("invalid", "card_height must be between 100 and 4000 pixels")
    return number


def _clean_str(value: Any, limit: int, what: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ArtifactError("invalid", f"{what} must be a string")
    value = value.strip()
    if not value and not allow_empty:
        raise ArtifactError("invalid", f"{what} must not be empty")
    if len(value) > limit:
        raise ArtifactError("invalid", f"{what} is longer than {limit} characters")
    return value


# --------------------------------------------------------------------------
# Find/replace edits
# --------------------------------------------------------------------------

MAX_EDITS = 64


def apply_edits(html: str, edits: Any) -> str:
    """Apply [{old_str, new_str}, ...] in order; each old_str must match exactly once.

    The same contract as a code editor's find/replace tool: ambiguous or missing
    matches are errors (so a slip never silently edits the wrong place), and the
    result is all-or-nothing because the caller only sees the final string.
    """
    if not isinstance(edits, list) or not edits:
        raise ArtifactError("invalid", "edits must be a non-empty list of {old_str, new_str}")
    if len(edits) > MAX_EDITS:
        raise ArtifactError("invalid", f"at most {MAX_EDITS} edits per patch")
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            raise ArtifactError("invalid", f"edit {index} must be an object with old_str and new_str")
        old, new = edit.get("old_str"), edit.get("new_str")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ArtifactError("invalid", f"edit {index} needs string old_str and new_str")
        if old == "":
            raise ArtifactError("invalid", f"edit {index}: old_str must not be empty")
        if old == new:
            raise ArtifactError("invalid", f"edit {index}: old_str and new_str are identical")
        count = html.count(old)
        if count != 1:
            head = old.strip().splitlines()[0][:60] if old.strip() else repr(old[:30])
            raise ArtifactError(
                "edit_failed",
                f"edit {index}: old_str matched {count} times (it must match exactly once); "
                f"starts with: {head!r}. "
                + ("Add surrounding text to make it unique." if count else "Check whitespace and quotes against artifact_read."),
                edit=index,
                matches=count,
            )
        html = html.replace(old, new, 1)
    return html


# --------------------------------------------------------------------------
# Static lint: things that will not work inside the sandbox, found without a browser
# --------------------------------------------------------------------------

_SCRIPT_HOSTS = ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com")
_STYLE_HOSTS = ("fonts.googleapis.com", "cdnjs.cloudflare.com")


def _host(url: str) -> str:
    match = re.match(r"https?://([^/:?#]+)", url, re.I)
    return match.group(1).lower() if match else ""


def lint_html(
    html: str, caps_requested: Any = None, display: str | None = None, card_height: Any = None
) -> list[dict[str, str]]:
    """Heuristic warnings; they never block a publish. Each is {level, code, message}."""
    out: list[dict[str, str]] = []
    wants_network = bool((caps_requested or {}).get("network"))

    def add(level: str, code: str, message: str) -> None:
        out.append({"level": level, "code": code, "message": message})

    code_only = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    if re.search(r"\b(localStorage|sessionStorage|indexedDB)\b|document\.cookie", code_only):
        add("warning", "storage",
            "localStorage, sessionStorage, indexedDB and cookies throw in the sandbox (no same-origin). "
            "Use ha.kv for state that must survive.")
    if re.search(r"\bfetch\s*\(|XMLHttpRequest|new\s+WebSocket|new\s+EventSource", code_only) and not wants_network:
        add("warning", "network",
            "fetch/XHR/WebSocket are blocked unless the page requests the 'network' capability "
            "and the user approves it.")
    for tag in re.findall(r"<script\b[^>]*\bsrc\s*=\s*[\"']?(https?://[^\"'\s>]+)", code_only, re.I):
        if _host(tag) not in _SCRIPT_HOSTS:
            add("error", "script_src",
                f"script {tag} is blocked: external scripts are only allowed from "
                "cdnjs.cloudflare.com, cdn.jsdelivr.net and unpkg.com.")
    for tag in re.findall(r"<link\b[^>]*\bhref\s*=\s*[\"']?(https?://[^\"'\s>]+)", code_only, re.I):
        if _host(tag) not in _STYLE_HOSTS and not re.search(r"fonts\.gstatic\.com", tag):
            add("error", "link_href",
                f"{tag} is blocked: external styles are only allowed from fonts.googleapis.com "
                "and cdnjs.cloudflare.com.")
    if not wants_network and re.search(r"<(img|video|audio|source)\b[^>]*\bsrc\s*=\s*[\"']?https?://", code_only, re.I):
        add("warning", "external_media",
            "external images/media are blocked without the 'network' capability; embed them as data: URIs.")
    if re.search(r"\bwindow\.open\s*\(|target\s*=\s*[\"']?_blank", code_only):
        add("warning", "popup", "popups and target=_blank links are blocked in the sandbox.")
    if re.search(r"\bha\.(kv|timers|entities|callService|notify)\b", code_only) and not re.search(r"\bha\.ready\b", code_only):
        add("warning", "ha_ready", "the page uses window.ha but never awaits ha.ready.")
    if code_only.lower().count("<script") != code_only.lower().count("</script"):
        add("error", "script_tag", "unbalanced <script> tags: the page's JavaScript will not run as written.")
    if not re.search(r"<meta[^>]+name\s*=\s*[\"']?viewport", code_only, re.I):
        add("info", "viewport", 'no <meta name="viewport">: the page will render zoomed-out on phones.')
    if display == "card" and isinstance(card_height, int):
        tall = [int(n) for n in re.findall(r"(?<![-\w])(?:min-)?height\s*:\s*(\d{3,4})px", code_only)]
        if tall and max(tall) > card_height * 1.1:
            add("warning", "card_tall",
                f"this is a dashboard card of {card_height}px but the page sets a {max(tall)}px height: "
                "keep a card compact (no inner scrolling) or make it a page.")
    return out


# --------------------------------------------------------------------------
# Render reports: what real browsers saw when they ran an artifact
# --------------------------------------------------------------------------

MAX_REPORT_ERRORS = 30


class RenderReports(_Listeners):
    """Latest render outcome per artifact, for the current version only (memory, not persisted)."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        super().__init__()
        self._clock = clock
        self._data: dict[str, dict[str, Any]] = {}

    def add(
        self,
        artifact_id: str,
        version: int,
        *,
        source: str,
        settled: bool,
        errors: list[dict[str, Any]] | None = None,
        frames: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        report = self._data.get(artifact_id)
        if report is None or report["version"] != version:
            report = {"version": version, "renders": 0, "settled": False, "sources": [], "errors": [],
                      "frames": [], "updated": 0.0}
            self._data[artifact_id] = report
        if settled:
            report["renders"] += 1
            report["settled"] = True
        if source not in report["sources"]:
            report["sources"].append(source)
        for raw in frames or []:
            if isinstance(raw, dict) and all(isinstance(raw.get(k), (int, float)) for k in ("w", "h", "content")):
                frame = {k: int(raw[k]) for k in ("w", "h", "content")}
                report["frames"] = [f for f in report["frames"] if (f["w"], f["h"]) != (frame["w"], frame["h"])][-5:]
                report["frames"].append(frame)
        seen = {(e["level"], e["message"]) for e in report["errors"]}
        for raw in errors or []:
            if not isinstance(raw, dict):
                continue
            entry = {
                "level": str(raw.get("level") or "error")[:20],
                "message": str(raw.get("message") or "")[:500],
            }
            if isinstance(raw.get("line"), int) and raw["line"] > 0:
                entry["line"] = raw["line"]
            key = (entry["level"], entry["message"])
            if key in seen or not entry["message"] or len(report["errors"]) >= MAX_REPORT_ERRORS:
                continue
            seen.add(key)
            report["errors"].append(entry)
        report["updated"] = self._clock()
        self._emit({"type": "render_report", "artifact_id": artifact_id, "version": version})
        return self.summary(artifact_id, version) or {}

    def summary(self, artifact_id: str, version: int) -> dict[str, Any] | None:
        report = self._data.get(artifact_id)
        if report is None or report["version"] != version:
            return None
        return copy.deepcopy(report)

    def forget(self, artifact_id: str) -> None:
        self._data.pop(artifact_id, None)


# --------------------------------------------------------------------------
# Artifact store
# --------------------------------------------------------------------------

class ArtifactStore(_Listeners):
    """Versioned artifacts, their grants and their shared key/value state."""

    def __init__(self, store: Any, clock: Callable[[], float] = time.time) -> None:
        super().__init__()
        self._store = store
        self._clock = clock
        self._data: dict[str, Any] = {"artifacts": {}, "kv": {}}

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._data = {
                "artifacts": data.get("artifacts") or {},
                "kv": data.get("kv") or {},
            }

    def _save(self) -> None:
        self._store.async_delay_save(lambda: self._data, 1.0)

    # -- reading --------------------------------------------------------

    @staticmethod
    def summary(artifact: dict) -> dict[str, Any]:
        keys = (
            "id", "title", "description", "icon", "version", "created",
            "updated", "updated_by", "source", "caps_requested", "caps_granted",
        )
        out = copy.deepcopy({key: artifact[key] for key in keys})
        out["display"] = artifact.get("display") or "both"  # artifacts made before this field
        out["card_height"] = artifact.get("card_height")
        out["size"] = len(artifact["html"].encode("utf-8"))
        return out

    def _require(self, artifact_id: str) -> dict:
        artifact = self._data["artifacts"].get(artifact_id)
        if artifact is None:
            raise ArtifactError("not_found", f"artifact '{artifact_id}' does not exist")
        return artifact

    def exists(self, artifact_id: str) -> bool:
        return artifact_id in self._data["artifacts"]

    def version_of(self, artifact_id: str) -> int | None:
        artifact = self._data["artifacts"].get(artifact_id)
        return artifact["version"] if artifact else None

    def list(self) -> list[dict]:
        items = [self.summary(a) for a in self._data["artifacts"].values()]
        return sorted(items, key=lambda a: a["updated"], reverse=True)

    def get(self, artifact_id: str) -> dict:
        artifact = self._require(artifact_id)
        return {**self.summary(artifact), "html": artifact["html"]}

    def history(self, artifact_id: str) -> list[dict]:
        artifact = self._require(artifact_id)
        return [
            {
                "version": h["version"],
                "updated": h["updated"],
                "updated_by": h["updated_by"],
                "source": h["source"],
                "size": len(h["html"]),
            }
            for h in reversed(artifact["history"])
        ]

    def get_grants(self, artifact_id: str) -> dict[str, Any]:
        artifact = self._data["artifacts"].get(artifact_id)
        return copy.deepcopy(artifact["caps_granted"]) if artifact else empty_grants()

    # -- writing --------------------------------------------------------

    def save(
        self,
        artifact_id: str,
        html: str,
        *,
        title: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        display: str | None = None,
        card_height: Any = None,
        base_version: int | None = None,
        source: str = "user",
        updated_by: str | None = None,
        caps_requested: Any = None,
    ) -> dict:
        """Create or update an artifact.

        Non-user sources (Claude over MCP, the REST API) must pass the version
        they last read, so they never overwrite a hand edit without seeing it.
        """
        if not isinstance(artifact_id, str) or not SLUG_RE.match(artifact_id):
            raise ArtifactError(
                "invalid", "artifact id must be lowercase letters, digits, '-' or '_' (max 64)"
            )
        if not isinstance(html, str):
            raise ArtifactError("invalid", "html must be a string")
        if len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise ArtifactError("too_large", f"html is larger than {MAX_HTML_BYTES} bytes")
        title = _clean_str(title, 120, "title") if title is not None else None
        description = (
            _clean_str(description, 500, "description", allow_empty=True)
            if description is not None else None
        )
        icon = _clean_str(icon, 64, "icon", allow_empty=True) if icon is not None else None
        requested = normalize_grants(caps_requested) if caps_requested is not None else None
        display = _clean_display(display)
        card_height = _clean_card_height(card_height)

        current = self._data["artifacts"].get(artifact_id)
        if current is not None:
            if source != "user" and base_version is None:
                raise ArtifactError(
                    "conflict",
                    "artifact already exists: read it first and pass base_version",
                    current=self.get(artifact_id),
                )
            if base_version is not None and base_version != current["version"]:
                raise ArtifactError(
                    "conflict",
                    f"artifact is at version {current['version']} (edited by "
                    f"{current['updated_by']}); you based your change on {base_version}",
                    current=self.get(artifact_id),
                )

        now = self._clock()
        by = updated_by or source
        if current is None:
            artifact = {
                "id": artifact_id,
                "title": title or artifact_id,
                "description": description or "",
                "icon": icon or "",
                "display": display or "page",
                "card_height": card_height,
                "html": html,
                "version": 1,
                "created": now,
                "updated": now,
                "updated_by": by,
                "source": source,
                "caps_requested": requested or empty_grants(),
                "caps_granted": empty_grants(),
                "history": [],
            }
            self._data["artifacts"][artifact_id] = artifact
        else:
            artifact = current
            unchanged = (
                html == artifact["html"]
                and (title is None or title == artifact["title"])
                and (description is None or description == artifact["description"])
                and (icon is None or icon == artifact["icon"])
                and (requested is None or requested == artifact["caps_requested"])
            )
            if unchanged:
                # Display type and card height are settings, not content: no new version.
                changed = False
                if display is not None and display != (artifact.get("display") or "both"):
                    artifact["display"], changed = display, True
                if card_height is not None and card_height != artifact.get("card_height"):
                    artifact["card_height"], changed = card_height, True
                if changed:
                    self._save()
                    self._emit({"type": "artifact", "change": "meta", "artifact": self.summary(artifact)})
                return self.get(artifact_id)
            artifact["history"].append(
                {
                    "version": artifact["version"],
                    "html": artifact["html"],
                    "title": artifact["title"],
                    "updated": artifact["updated"],
                    "updated_by": artifact["updated_by"],
                    "source": artifact["source"],
                    "caps_requested": artifact["caps_requested"],
                }
            )
            del artifact["history"][:-MAX_HISTORY]
            artifact["html"] = html
            artifact["version"] += 1
            artifact["updated"] = now
            artifact["updated_by"] = by
            artifact["source"] = source
            if title is not None:
                artifact["title"] = title
            if description is not None:
                artifact["description"] = description
            if icon is not None:
                artifact["icon"] = icon
            if requested is not None:
                artifact["caps_requested"] = requested
            if display is not None:
                artifact["display"] = display
            if card_height is not None:
                artifact["card_height"] = card_height

        self._save()
        self._emit({"type": "artifact", "change": "save", "artifact": self.summary(artifact)})
        return self.get(artifact_id)

    def patch(
        self,
        artifact_id: str,
        edits: Any,
        *,
        base_version: int | None,
        title: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        display: str | None = None,
        card_height: Any = None,
        caps_requested: Any = None,
        source: str = "user",
        updated_by: str | None = None,
    ) -> dict:
        """Apply find/replace edits to the current html and save the result as a new version.

        The edits are checked against the version the caller read (`base_version`), never
        against whatever is stored now, so a hand edit in between is a conflict instead of
        a silent mis-application. All edits apply or none do.
        """
        current = self._require(artifact_id)
        if base_version is None:
            raise ArtifactError(
                "conflict",
                "pass the base_version you read (artifact_read with metadata_only is enough)",
                current=self.summary(current),
            )
        if base_version != current["version"]:
            raise ArtifactError(
                "conflict",
                f"artifact is at version {current['version']} (edited by "
                f"{current['updated_by']}); your edits are based on {base_version}",
                current=self.get(artifact_id),
            )
        html = apply_edits(current["html"], edits)
        return self.save(
            artifact_id,
            html,
            title=title,
            description=description,
            icon=icon,
            display=display,
            card_height=card_height,
            base_version=base_version,
            caps_requested=caps_requested,
            source=source,
            updated_by=updated_by,
        )

    def revert(self, artifact_id: str, version: int, updated_by: str = "user") -> dict:
        artifact = self._require(artifact_id)
        entry = next((h for h in artifact["history"] if h["version"] == version), None)
        if entry is None:
            raise ArtifactError("not_found", f"version {version} is not in the history")
        return self.save(
            artifact_id,
            entry["html"],
            title=entry["title"],
            caps_requested=entry["caps_requested"],
            base_version=artifact["version"],
            source="user",
            updated_by=updated_by,
        )

    def set_grants(self, artifact_id: str, grants: Any) -> dict:
        artifact = self._require(artifact_id)
        artifact["caps_granted"] = normalize_grants(grants)
        self._save()
        self._emit({"type": "artifact", "change": "grants", "artifact": self.summary(artifact)})
        return self.summary(artifact)

    def delete(self, artifact_id: str) -> None:
        artifact = self._require(artifact_id)
        summary = self.summary(artifact)
        del self._data["artifacts"][artifact_id]
        self._data["kv"].pop(artifact_id, None)
        self._save()
        self._emit({"type": "artifact", "change": "delete", "artifact": summary})

    # -- key/value state --------------------------------------------------

    def kv_all(self, artifact_id: str) -> dict[str, Any]:
        self._require(artifact_id)
        return copy.deepcopy(self._data["kv"].get(artifact_id, {}))

    def kv_get(self, artifact_id: str, key: str) -> Any:
        self._require(artifact_id)
        return copy.deepcopy(self._data["kv"].get(artifact_id, {}).get(key))

    def kv_set(self, artifact_id: str, key: str, value: Any) -> None:
        self._require(artifact_id)
        key = _clean_str(key, 128, "key")
        try:
            size = len(json.dumps(value))
        except (TypeError, ValueError) as err:
            raise ArtifactError("invalid", "value must be JSON serialisable") from err
        if size > MAX_KV_VALUE_BYTES:
            raise ArtifactError("too_large", f"value is larger than {MAX_KV_VALUE_BYTES} bytes")
        bucket = self._data["kv"].setdefault(artifact_id, {})
        if key not in bucket and len(bucket) >= MAX_KV_KEYS:
            raise ArtifactError("limit", f"at most {MAX_KV_KEYS} keys per artifact")
        bucket[key] = copy.deepcopy(value)
        self._save()
        self._emit({"type": "kv", "artifact_id": artifact_id, "key": key, "value": value, "deleted": False})

    def kv_delete(self, artifact_id: str, key: str) -> None:
        self._require(artifact_id)
        bucket = self._data["kv"].get(artifact_id, {})
        if key in bucket:
            del bucket[key]
            self._save()
            self._emit({"type": "kv", "artifact_id": artifact_id, "key": key, "value": None, "deleted": True})


# --------------------------------------------------------------------------
# Timer engine
# --------------------------------------------------------------------------

DEFAULT_TRACK = "main"
JOIN_TRACK = "*"
TRACK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_TRACKS = 10
MAX_TIMER_LOG = 200
MAX_REGATES = 30
YES_WORDS = {"yes", "y", "done", "true", "ok"}
ADVANCE_MODES = ("auto", "manual")


def _remind_seconds(value: Any) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as err:
        raise ArtifactError("invalid", "remindEvery must be a number of seconds") from err
    if not 10 <= seconds <= 3600:
        raise ArtifactError("invalid", "remindEvery must be between 10 and 3600 seconds")
    return seconds


def _advance_mode(value: Any) -> str:
    if value not in ADVANCE_MODES:
        raise ArtifactError("invalid", 'advance must be "auto" or "manual"')
    return value


def _normalize_tracks(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_TRACKS:
        raise ArtifactError("invalid", f"tracks must be a list of at most {MAX_TRACKS}")
    out: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict):
            raise ArtifactError("invalid", "each track must be an object like {id, label}")
        tid = str(item.get("id", ""))
        if not TRACK_ID_RE.match(tid):
            raise ArtifactError("invalid", "track ids use letters, digits, - and _ (max 32 characters)")
        if any(t["id"] == tid for t in out):
            raise ArtifactError("invalid", f"track '{tid}' is listed twice")
        out.append({"id": tid, "label": _clean_str(item.get("label") or tid, 60, "track label")})
    return out


def _normalize_gate(raw: Any) -> dict:
    if raw is True:
        raw = {}
    if not isinstance(raw, dict):
        raise ArtifactError("invalid", "gate must be an object like {question, yes, more}")
    more_raw = raw.get("more", [60, 120])
    if not isinstance(more_raw, list) or len(more_raw) > 2:
        raise ArtifactError("invalid", "gate.more is a list of at most 2 extra-time options (seconds)")
    more = []
    for value in more_raw:
        try:
            seconds = int(value)
        except (TypeError, ValueError) as err:
            raise ArtifactError("invalid", "gate.more entries are numbers of seconds") from err
        if not 1 <= seconds <= 3600:
            raise ArtifactError("invalid", "gate.more entries must be between 1 and 3600 seconds")
        more.append(seconds)
    gate = {
        "question": _clean_str(raw.get("question") or "Done yet?", 200, "gate question"),
        "yes": _clean_str(raw.get("yes") or "Done", 40, "gate yes label"),
        "more": more,
    }
    if raw.get("autoYesAfter") is not None:
        try:
            auto = float(raw["autoYesAfter"])
        except (TypeError, ValueError) as err:
            raise ArtifactError("invalid", "autoYesAfter must be a number of seconds") from err
        if not 1 <= auto <= MAX_SECONDS:
            raise ArtifactError("invalid", "autoYesAfter is out of range")
        gate["autoYesAfter"] = auto
    return gate


class TimerEngine(_Listeners):
    """Server-side, restart-safe timers with milestone steps, tracks and gates.

    A timer has ``steps`` (each firing ``at`` seconds after start) grouped into
    parallel *tracks*. The timer keeps one clock ``T`` (banked while the whole timer
    is paused). Each track derives its own time from ``T``: ``T - held``, where
    ``held`` is how long that track has been paused (by a check gate or by the user).
    Steps without a track live on one implicit track, so flat timers behave exactly as
    they always did.

    A step may also have a ``duration`` (an activity, optionally announced when it ends
    via ``endLabel``), a ``gate`` ("cook until done": pauses its track until answered)
    or ``after`` (a join step that waits for other tracks to finish). A step at offset 0
    is shown immediately and not announced.

    Events emitted to listeners:
      {"type": "timer", "change": ..., "timer": view, "server_now": ..., [track, ...]}
      {"type": "timer_fired", "kind": "step" | "gate" | "finished", "phase": "start" | "end",
       "timer": view, "step": {...} | None, "index": int | None, "track": str | None,
       "missed": int}
    """

    def __init__(
        self,
        store: Any,
        clock: Callable[[], float],
        scheduler: Callable[[float, Callable[[], None]], Callable[[], None]],
    ) -> None:
        super().__init__()
        self._store = store
        self._clock = clock
        self._scheduler = scheduler
        self._timers: dict[str, dict] = {}
        self._cancels: dict[str, Callable[[], None]] = {}

    # -- lifecycle -------------------------------------------------------

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        now = self._clock()
        for tid, timer in (data.get("timers") or {}).items():
            if timer.get("state") == "finished" and now - timer.get("finished_at", now) > FINISHED_RETENTION:
                continue
            self._migrate(timer)
            self._upgrade(timer)
            self._timers[tid] = timer
        for timer in self._timers.values():
            if timer["state"] == "running":
                self._arm(timer)  # overdue timers fire immediately (catch-up)

    def async_shutdown(self) -> None:
        for cancel in self._cancels.values():
            cancel()
        self._cancels.clear()

    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"timers": self._timers}, 0.5)

    @staticmethod
    def _migrate(timer: dict) -> None:
        """Bring a timer saved by an older version up to the track model."""
        if "tstate" in timer:
            return
        steps = timer.get("steps") or []
        for step in steps:
            step.setdefault("track", DEFAULT_TRACK)
            step.setdefault("plan_at", step["at"])
        last = max((s["at"] for s in steps), default=0.0)
        timer["tracks"] = [{"id": DEFAULT_TRACK, "label": "Main"}]
        timer["multi"] = False
        timer["auto_duration"] = abs(timer.get("duration", 0) - last) < EPS
        timer["planned_duration"] = timer.get("duration", 0)
        timer["history"] = []
        timer["started_wall"] = timer.get("started_wall")
        timer["tstate"] = {DEFAULT_TRACK: TimerEngine._new_tstate(timer.pop("fired", 0))}
        timer.pop("fired", None)

    @staticmethod
    def _upgrade(timer: dict) -> None:
        """Fields added after 0.4.0 (original schedule for reset, step log, overtime)."""
        timer.setdefault("orig_steps", copy.deepcopy(timer["steps"]))
        timer.setdefault("orig_duration", timer["duration"])
        timer.setdefault("step_log", [])
        for ts in timer["tstate"].values():
            ts.setdefault("ot", None)
            ts.setdefault("rem", {})

    @staticmethod
    def _is_manual(timer: dict, step: dict) -> bool:
        return (step.get("advance") or timer.get("advance") or "auto") == "manual"

    @staticmethod
    def _remind_every(timer: dict, step: dict) -> float | None:
        return step.get("remindEvery") or timer.get("remind_every") or None

    def _armed(self, timer: dict, tid: str, ts: dict, index: int) -> bool:
        """A step-anchored reminder only runs once the step it sits under has started."""
        steps = timer["steps"]
        reminder = steps[index]
        parent = None
        for number, step in enumerate(steps):
            if step["track"] == tid and not step.get("reminder") and step["at"] <= reminder["at"] + EPS:
                parent = number
        if parent is None:
            return True
        events = self._events(timer, tid)
        return any(e[3] == parent and e[2] != "end" for e in events[: ts["ev"]])

    def _pending_reminders(self, timer: dict, tid: str, ts: dict) -> list[tuple[float, int]]:
        """(timer-clock due time, step index) of reminders in a lane that have not fired yet.

        anchor "step" reminders run on the lane's own clock (so they move with overtime and an
        early Next, and freeze while the lane is held); anchor "timer" ones are fixed offsets
        from the start of the timer."""
        out = []
        for index, step in enumerate(timer["steps"]):
            if not step.get("reminder") or step["track"] != tid or str(index) in ts["rem"]:
                continue
            if step.get("anchor") == "timer":
                out.append((step["at"], index))
            elif ts["hold_since"] is None and self._armed(timer, tid, ts, index):
                out.append((step["at"] + ts["held"], index))
        out.sort()
        return out

    def _cur_step(self, timer: dict, events: list, upto: int) -> int | None:
        """Index of the step the lane is on after the first ``upto`` events."""
        steps, cur = timer["steps"], None
        for time_, _order, kind, index in events[:upto]:
            if kind == "end":
                continue
            cur = index
        return cur

    def _manual_hold(self, timer: dict, tid: str, events: list, ev: int) -> dict | None:
        """Describe a manual step waiting for "Next": its index, when its time is up, and whether
        the next pending event (an end-of-activity alert) may still fire on time."""
        if tid == JOIN_TRACK:
            return None
        cur = self._cur_step(timer, events, ev)
        steps = timer["steps"]
        if cur is None or steps[cur].get("gate") or not self._is_manual(timer, steps[cur]):
            return None
        if timer["tstate"][tid].get("rel") == cur:
            return None  # Next already ended this step; what follows is about to be handled
        j = ev
        while j < len(events) and events[j][2] == "end":
            j += 1  # an end-of-activity alert still fires on time
        step = steps[cur]
        if j >= len(events):
            # The last step of its lane has nothing after it to mark where it ends, so a step with
            # a duration holds from its planned end until Next (boundary == len(events)).
            if not step.get("duration"):
                return None
            return {"cur": cur, "end": step["at"] + step["duration"], "free": j > ev, "boundary": j}
        end = step["at"] + step["duration"] if step.get("duration") else events[j][0]
        return {"cur": cur, "end": end, "free": j > ev, "boundary": j}

    @staticmethod
    def _new_tstate(ev: int = 0) -> dict:
        return {"ev": ev, "held": 0.0, "hold_since": None, "waiting": None, "manual": False,
                "auto_at": None, "offset": 0.0, "regates": [], "ot": None, "rem": {}, "rel": None}

    # -- low-level model -----------------------------------------------------

    @staticmethod
    def _events(timer: dict, tid: str) -> list[tuple]:
        """Sorted (time, order, kind, step index) tuples of one track."""
        events: list[tuple] = []
        for index, step in enumerate(timer["steps"]):
            if step["track"] != tid or step.get("reminder"):
                continue  # reminders are not part of the sequence: they never become current
            events.append((step["at"], 0, "start", index))
            if step.get("duration") and step.get("endLabel") and not step.get("gate"):
                events.append((step["at"] + step["duration"], 1, "end", index))
        for regate in timer["tstate"][tid]["regates"]:
            events.append((regate["at"], 0, "gate", regate["index"]))
        events.sort(key=lambda e: (e[0], e[1], e[3]))
        return events

    @staticmethod
    def _track_end(timer: dict, tid: str) -> float:
        end = 0.0
        for step in timer["steps"]:
            if step["track"] == tid and not step.get("reminder"):
                end = max(end, step["at"] + (0 if step.get("gate") else step.get("duration") or 0))
        for regate in timer["tstate"][tid]["regates"]:
            end = max(end, regate["at"])
        return end

    def _plan_end(self, timer: dict) -> float:
        end = 0.0
        for tid in timer["tstate"]:
            end = max(end, self._track_end(timer, tid))
        return end

    def _elapsed(self, timer: dict, now: float) -> float:
        elapsed = timer["elapsed"]
        if timer["state"] == "running":
            elapsed += now - timer["started_at"]
        return max(0.0, elapsed)

    @staticmethod
    def _tel(timer: dict, tid: str, big_t: float) -> float:
        ts = timer["tstate"][tid]
        if ts["hold_since"] is not None:
            return max(0.0, ts["hold_since"] - ts["held"])
        return max(0.0, big_t - ts["held"])

    def _tail_hold(self, timer: dict, tid: str) -> dict | None:
        """The hold of a lane whose last step is manual and has not been ended with Next yet."""
        events = self._events(timer, tid)
        hold = self._manual_hold(timer, tid, events, timer["tstate"][tid]["ev"])
        return hold if hold and hold["boundary"] >= len(events) else None

    def _track_done(self, timer: dict, tid: str, big_t: float) -> bool:
        ts = timer["tstate"][tid]
        return (
            ts["ev"] >= len(self._events(timer, tid))
            and ts["waiting"] is None
            and self._tail_hold(timer, tid) is None
            and self._tel(timer, tid, big_t) >= self._track_end(timer, tid) - EPS
        )

    def _join_ready_at(self, timer: dict, step: dict) -> float | None:
        """Timer-clock moment a join step can fire, or None while a dependency is blocked."""
        ready = 0.0
        for dep in step.get("after") or []:
            ts = timer["tstate"].get(dep)
            if ts is None:
                continue
            if ts["waiting"] is not None or ts["hold_since"] is not None:
                return None
            if ts.get("ot") is not None or self._tail_hold(timer, dep) is not None:
                return None  # that lane is still waiting for "Next"
            ready = max(ready, self._track_end(timer, dep) + ts["held"])
        return ready

    def _finished(self, timer: dict, big_t: float) -> bool:
        longest = 0.0
        for tid, ts in timer["tstate"].items():
            if ts["waiting"] is not None or ts["ev"] < len(self._events(timer, tid)):
                return False
            if self._tail_hold(timer, tid) is not None:
                return False  # the last step waits for "Next"
            longest = max(longest, self._tel(timer, tid, big_t))
        return longest >= timer["duration"] - EPS

    def _fix_duration(self, timer: dict, delta: float | None = None, big_t: float = 0.0) -> None:
        if timer.get("auto_duration"):
            timer["duration"] = max(1.0, self._plan_end(timer))
        elif delta is not None:
            timer["duration"] = max(big_t + 1.0, timer["duration"] + delta)
        else:
            timer["duration"] = max(timer["duration"], self._plan_end(timer))

    def _log(self, timer: dict, action: str, now: float, **fields: Any) -> None:
        entry = {"t": now, "action": action}
        entry.update({k: v for k, v in fields.items() if v is not None})
        history = timer.setdefault("history", [])
        history.append(entry)
        del history[:-MAX_TIMER_LOG]

    # -- views -------------------------------------------------------------

    def _track_view(self, timer: dict, tid: str, big_t: float, now: float) -> dict:
        ts = timer["tstate"][tid]
        steps = timer["steps"]
        events = self._events(timer, tid)
        el = self._tel(timer, tid, big_t)
        ep = ts["ev"]
        if tid != JOIN_TRACK and ts["waiting"] is None and ts["hold_since"] is None:
            while ep < len(events) and events[ep][0] <= el + EPS:
                hold = self._manual_hold(timer, tid, events, ep)
                if hold and not hold["free"]:
                    break  # waits for "Next", however late
                ep += 1  # due but not announced yet: already "current" on screen
        cur_idx = self._cur_step(timer, events, ep)
        cur = next((e for e in reversed(events[:ep]) if e[3] == cur_idx and e[2] != "end"), None) if cur_idx is not None else None
        nxt = events[ep] if ep < len(events) else None
        end = self._track_end(timer, tid)
        if timer["state"] == "idle":
            state = "idle"
        elif timer["state"] == "finished":
            state = "finished"
        elif ts["waiting"] is not None:
            state = "waiting"
        elif ts["hold_since"] is not None or timer["state"] == "paused":
            state = "paused"
        elif nxt is None and el >= end - EPS and self._manual_hold(timer, tid, events, ep) is None:
            state = "done"
        else:
            state = "running"
        next_in = None
        if nxt is not None and state != "waiting":
            if tid == JOIN_TRACK:
                ready = self._join_ready_at(timer, steps[nxt[3]])
                next_in = None if ready is None else max(0.0, max(nxt[0], ready) - big_t)
            else:
                next_in = max(0.0, nxt[0] - el)
        remaining = None
        current_index = cur[3] if cur else -1
        hold = self._manual_hold(timer, tid, events, ep)
        overtime = 0.0
        if hold:
            remaining = hold["end"] - el  # negative once the step runs over
            if ts.get("ot") is not None:
                overtime = max(0.0, el - hold["end"])
        elif cur and cur[2] != "end" and not steps[current_index].get("gate") and steps[current_index].get("duration"):
            remaining = max(0.0, steps[current_index]["at"] + steps[current_index]["duration"] - el)
        waiting = ts["waiting"]
        label = next((t["label"] for t in timer["tracks"] if t["id"] == tid), tid)
        out = {
            "id": tid,
            "label": label,
            "state": state,
            "current_index": current_index,
            "next_index": nxt[3] if nxt else None,
            "next_kind": nxt[2] if nxt else None,
            "next_in": None if next_in is None else round(next_in, 3),
            "remaining": None if remaining is None else round(remaining, 3),
            "manual": hold is not None,
            "overtime": round(overtime, 3),
            "elapsed": round(el, 3),
            "offset": round(ts["offset"], 3),
            "waiting": waiting,
            "gate": copy.deepcopy(steps[waiting]["gate"]) if waiting is not None else None,
        }
        if waiting is not None and ts.get("auto_at") is not None:
            out["auto_in"] = round(max(0.0, ts["auto_at"] - big_t), 3)
        return out

    def view(self, timer: dict) -> dict[str, Any]:
        now = self._clock()
        big_t = self._elapsed(timer, now)
        steps = timer["steps"]
        running = timer["state"] == "running"
        views = {tid: self._track_view(timer, tid, big_t, now) for tid in timer["tstate"]}
        longest = max((v["elapsed"] for v in views.values()), default=0.0)
        remaining = max(0.0, timer["duration"] - longest)
        # The soonest upcoming event across tracks drives the old top-level fields.
        pick = None
        for v in views.values():
            if v["state"] in ("waiting",) or v["next_in"] is None or v["next_index"] is None:
                continue
            if pick is None or (v["next_in"], v["next_index"]) < (pick["next_in"], pick["next_index"]):
                pick = v
        waiting = [{"track": v["id"], "index": v["waiting"]} for v in views.values() if v["waiting"] is not None]
        if pick is not None:
            current, nxt, next_in = pick["current_index"], pick["next_index"], pick["next_in"]
            if current < 0:
                current = max((v["current_index"] for v in views.values()), default=-1)
        elif waiting:
            current, nxt, next_in = waiting[0]["index"], None, remaining
        else:
            current, nxt, next_in = max((v["current_index"] for v in views.values()), default=-1), None, remaining
        real = [views[t["id"]] for t in timer["tracks"] if t["id"] in views]
        multi = bool(timer.get("multi"))
        reminders = []
        for index, step in enumerate(steps):
            if not step.get("reminder"):
                continue
            ts = timer["tstate"].get(step["track"])
            record = ts["rem"].get(str(index)) if ts else None
            state = record["state"] if record else ("skipped" if timer["state"] == "finished" else "pending")
            due = None
            armed = state == "pending" and (step.get("anchor") == "timer" or self._armed(timer, step["track"], ts, index))
            if armed:
                base = big_t if step.get("anchor") == "timer" else views[step["track"]]["elapsed"]
                due = round(max(0.0, step["at"] - base), 3)
            reminders.append({
                "index": index, "label": step["label"], "detail": step.get("detail", ""),
                "track": step["track"] if multi else None, "anchor": step.get("anchor", "step"),
                "state": state, "armed": armed, "due": due, "firedAt": record.get("at") if record else None,
            })
        return {
            "id": timer["id"],
            "artifact_id": timer["artifact_id"],
            "label": timer["label"],
            "state": timer["state"],
            "steps": copy.deepcopy(steps),
            "duration": timer["duration"],
            "notify_targets": list(timer.get("notify_targets") or []),
            "created": timer["created"],
            "elapsed_now": round(big_t, 3),
            "remaining": round(remaining, 3),
            "current_index": current,
            "next_index": nxt,
            "next_in": round(next_in, 3),
            "next_due": now + next_in if running and (nxt is not None or not waiting) else None,
            "ends_at": now + remaining if running and not waiting else None,
            "server_now": now,
            "multi": bool(timer.get("multi")),
            "tracks": real,
            "reminders": reminders,
            "join": views.get(JOIN_TRACK),
            "waiting": waiting,
            "history": copy.deepcopy(timer.get("history") or []),
            "overtime": max((v["overtime"] for v in views.values()), default=0.0),
            "step_log": copy.deepcopy(timer.get("step_log") or []),
            "extra": self._extra_view(timer),
            "advance": timer.get("advance") or "auto",
            "planned_duration": timer.get("planned_duration", timer["duration"]),
            "started_wall": timer.get("started_wall"),
            "finished_wall": timer.get("finished_at"),
        }

    def _extra_view(self, timer: dict) -> list[dict]:
        """Time added per step: [{index, track, label, gate, extend, seconds}], in step order."""
        out = []
        for key in sorted(timer.get("extra") or {}, key=int):
            row = timer["extra"][key]
            if row["index"] >= len(timer["steps"]):
                continue
            out.append({
                "index": row["index"], "track": row["track"] if timer.get("multi") else None,
                "label": timer["steps"][row["index"]]["label"], "gate": row["gate"],
                "extend": row["extend"], "seconds": round(row["gate"] + row["extend"], 3),
            })
        return out

    def list(self, artifact_id: str | None = None) -> list[dict]:
        timers = [
            self.view(t) for t in self._timers.values()
            if artifact_id is None or t["artifact_id"] == artifact_id
        ]
        return sorted(timers, key=lambda t: t["created"])

    def get_view(self, timer_id: str) -> dict:
        return self.view(self._get(timer_id))

    def _get(self, timer_id: str, artifact_id: str | None = None) -> dict:
        timer = self._timers.get(timer_id)
        if timer is None or (artifact_id is not None and timer["artifact_id"] != artifact_id):
            raise ArtifactError("not_found", f"timer '{timer_id}' does not exist")
        return timer

    def _emit_state(self, timer: dict, change: str, **extra: Any) -> None:
        event = {"type": "timer", "change": change, "timer": self.view(timer), "server_now": self._clock()}
        event.update(extra)
        self._emit(event)

    def _track_arg(self, timer: dict, track: str | None) -> str | None:
        if track is None or track == "":
            return None
        if track not in timer["tstate"] or track == JOIN_TRACK:
            raise ArtifactError("invalid", f"timer has no track '{track}'")
        return track

    # -- scheduling ----------------------------------------------------------

    def _disarm(self, timer_id: str) -> None:
        cancel = self._cancels.pop(timer_id, None)
        if cancel:
            cancel()

    def _next_target(self, timer: dict) -> float | None:
        """Timer-clock moment of the next thing that needs attention (None = nothing scheduled)."""
        best: float | None = None
        pending = False
        soonest_reminder: float | None = None
        for tid, ts in timer["tstate"].items():
            if tid != JOIN_TRACK:
                due = self._pending_reminders(timer, tid, ts)
                if due:
                    soonest_reminder = due[0][0] if soonest_reminder is None else min(soonest_reminder, due[0][0])
            if ts["waiting"] is not None:
                pending = True
                if ts.get("auto_at") is not None:
                    best = ts["auto_at"] if best is None else min(best, ts["auto_at"])
                continue
            events = self._events(timer, tid)
            hold = self._manual_hold(timer, tid, events, ts["ev"])
            if ts["ev"] >= len(events) and not hold:
                continue
            pending = True
            if ts["hold_since"] is not None:
                continue
            if hold:  # may wait for "Next": schedule the overtime onset and reminders
                ot = ts.get("ot")
                cand = hold["end"] + ts["held"] if ot is None else ot.get("next_remind")
                if cand is not None:
                    best = cand if best is None else min(best, cand)
                if not hold["free"]:
                    continue
            when = events[ts["ev"]][0] + ts["held"]
            if tid == JOIN_TRACK:
                ready = self._join_ready_at(timer, timer["steps"][events[ts["ev"]][3]])
                if ready is None:
                    continue
                when = max(when, ready)
            best = when if best is None else min(best, when)
        if pending:
            base = best
        else:
            ends = [timer["duration"] + ts["held"] for ts in timer["tstate"].values() if ts["hold_since"] is None]
            base = min(ends) if ends else None
        if soonest_reminder is not None:
            return soonest_reminder if base is None else min(base, soonest_reminder)
        return base

    def _arm(self, timer: dict) -> None:
        self._disarm(timer["id"])
        if timer["state"] != "running":
            return
        target = self._next_target(timer)
        if target is None:
            return
        delay = max(0.0, target - self._elapsed(timer, self._clock()))
        timer_id = timer["id"]
        self._cancels[timer_id] = self._scheduler(delay, lambda: self._on_due(timer_id))

    def _release(self, ts: dict, big_t: float) -> None:
        if ts["hold_since"] is not None:
            ts["held"] += max(0.0, big_t - ts["hold_since"])
        ts.update(hold_since=None, waiting=None, manual=False, auto_at=None)

    def _collect(self, timer: dict, big_t: float) -> tuple[list[dict], int | None, bool]:
        """Consume every event that is due. Returns (fired, terminal step index, anything changed)."""
        steps, duration = timer["steps"], timer["duration"]
        fired: list[dict] = []
        terminal: int | None = None
        changed = False
        while True:
            best = None
            for tid, ts in timer["tstate"].items():
                due_reminders = self._pending_reminders(timer, tid, ts) if tid != JOIN_TRACK else []
                if due_reminders and due_reminders[0][0] <= big_t + EPS:
                    cand = (due_reminders[0][0], tid, "reminder", due_reminders[0][1])
                    if best is None or cand[0] < best[0]:
                        best = cand
                if ts["hold_since"] is not None:
                    if ts["waiting"] is not None and ts.get("auto_at") is not None and ts["auto_at"] <= big_t + EPS:
                        cand = (ts["auto_at"], tid, "auto", None)
                        if best is None or cand[0] < best[0]:
                            best = cand
                    continue
                events = self._events(timer, tid)
                hold = self._manual_hold(timer, tid, events, ts["ev"])
                if ts["ev"] >= len(events) and not hold:
                    continue
                if hold:
                    ot = ts.get("ot")
                    if ot is None:
                        onset = hold["end"] + ts["held"]
                        if onset <= big_t + EPS:
                            cand = (onset, tid, "overtime", hold["cur"])
                            if best is None or cand[0] < best[0]:
                                best = cand
                    else:
                        due = ot.get("next_remind")
                        if due is not None and due <= big_t + EPS:
                            cand = (due, tid, "remind", hold["cur"])
                            if best is None or cand[0] < best[0]:
                                best = cand
                    if not hold["free"]:
                        continue
                else:
                    ts["ot"] = None
                time_, _order, kind, index = events[ts["ev"]]
                when = time_ + ts["held"]
                if tid == JOIN_TRACK:
                    ready = self._join_ready_at(timer, steps[index])
                    if ready is None:
                        continue
                    when = max(when, ready)
                if when > big_t + EPS:
                    continue
                if best is None or when < best[0]:
                    best = (when, tid, kind, index)
            if best is None:
                break
            when, tid, kind, index = best
            ts = timer["tstate"][tid]
            changed = True
            if kind == "auto":
                self._release(ts, when)
                self._log(timer, "answer", self._clock(), track=tid, answer="auto", source="auto")
                continue
            if kind == "reminder":
                ts["rem"][str(index)] = {"state": "fired", "at": self._clock() - max(0.0, big_t - when)}
                fired.append({"kind": "reminder", "phase": "start", "index": index, "track": tid, "t": when})
                continue
            if kind in ("overtime", "remind"):
                every = self._remind_every(timer, steps[index])
                if kind == "overtime":
                    ts["ot"] = {"since": when, "idx": index, "sent": 0,
                                "next_remind": when + every if every else None}
                else:
                    ts["ot"]["sent"] += 1
                    ts["ot"]["next_remind"] = when + every * (1 + int(max(0.0, big_t - when) // every))
                fired.append({"kind": "overtime", "phase": "start", "index": index, "track": tid,
                              "t": when, "reminder": kind == "remind"})
                continue
            ts["ev"] += 1
            step = steps[index]
            if kind in ("start", "gate") and step.get("gate"):
                ts["hold_since"] = when
                ts["waiting"] = index
                auto = step["gate"].get("autoYesAfter")
                ts["auto_at"] = when + auto if auto else None
                fired.append({"kind": "gate", "phase": "start", "index": index, "track": tid, "t": when})
                continue
            if kind == "end":
                fired.append({"kind": "step", "phase": "end", "index": index, "track": tid, "t": when})
                continue
            rest = all(
                other["ev"] >= len(self._events(timer, other_id)) and other["waiting"] is None
                for other_id, other in timer["tstate"].items()
            )
            if not step.get("duration") and ((tid == JOIN_TRACK and rest) or (tid != JOIN_TRACK and step["at"] >= duration - EPS)):
                terminal = index  # the closing step is announced as "finished", not as a step
                continue
            fired.append({"kind": "step", "phase": "start", "index": index, "track": tid, "t": when})
        return fired, terminal, changed

    def _on_due(self, timer_id: str) -> None:
        self._cancels.pop(timer_id, None)
        timer = self._timers.get(timer_id)
        if timer is None or timer["state"] != "running":
            return
        self._settle(timer, self._clock(), None)

    def _settle(self, timer: dict, now: float, change: str | None, **extra: Any) -> dict:
        """Fire whatever is due, finish if everything is done, persist, re-arm, announce."""
        big_t = self._elapsed(timer, now)
        if timer["state"] == "running":
            fired, terminal, _changed = self._collect(timer, big_t)
        else:
            fired, terminal = [], None
        if timer["state"] == "running" and self._finished(timer, big_t):
            ended = [f for f in fired if f["phase"] == "end"]
            if terminal is None and ended:
                terminal = ended[-1]["index"]
            self._finish(timer, now, terminal)
            return self.view(timer)

        gates = [f for f in fired if f["kind"] == "gate"]
        late = {}
        for f in fired:
            if f["kind"] == "overtime":
                late[f["track"]] = f  # a long wait produces one overtime alert per lane, not a pile
        gates = gates + list(late.values())
        regular = [f for f in fired if f["kind"] not in ("gate", "overtime")]
        on_time = [f for f in regular if f["t"] >= big_t - LATE_AFTER]
        missed = len(regular) - len(on_time)
        to_fire = on_time
        if regular and not on_time:  # woke up long after: announce only the latest step
            to_fire = regular[-1:]
            missed = len(regular) - 1
        announce = sorted(to_fire + gates, key=lambda f: f["t"])

        self._save()
        self._arm(timer)
        label = change or (
            "gate" if any(f["kind"] == "gate" for f in fired)
            else "overtime" if late else "step" if fired else "tick"
        )
        self._emit_state(timer, label, **extra)
        multi = bool(timer.get("multi"))
        first = True
        for item in announce:
            step = timer["steps"][item["index"]]
            if item["kind"] not in ("gate", "overtime") and not step.get("notify", True):
                continue
            self._emit(
                {
                    "type": "timer_fired",
                    "kind": item["kind"],
                    "phase": item["phase"],
                    "timer": self.view(timer),
                    "step": copy.deepcopy(step),
                    "index": item["index"],
                    "track": item["track"] if multi else None,
                    "missed": missed if first and item["kind"] not in ("gate", "overtime") else 0,
                    "reminder": item.get("reminder", False),
                    "over": max(0.0, round(big_t - item["t"], 3)) if item["kind"] == "overtime" else 0.0,
                }
            )
            if item["kind"] not in ("gate", "overtime"):
                first = False
        return self.view(timer)

    def _finish(self, timer: dict, now: float, last_index: int | None = None) -> None:
        steps, duration = timer["steps"], timer["duration"]
        big_t = self._elapsed(timer, now)
        if last_index is not None:
            last = steps[last_index]
        else:
            last = steps[-1] if steps and steps[-1]["at"] >= duration - EPS else None
        self._disarm(timer["id"])
        for ts in timer["tstate"].values():
            ts.update(hold_since=None, waiting=None, auto_at=None, manual=False)
        timer.update(state="finished", elapsed=max(big_t, duration), started_at=None, finished_at=now)
        self._log(timer, "finish", now)
        self._save()
        self._emit_state(timer, "finish")
        self._emit(
            {
                "type": "timer_fired",
                "kind": "finished",
                "phase": "start",
                "timer": self.view(timer),
                "step": copy.deepcopy(last),
                "index": steps.index(last) if last is not None else None,
                "track": None,
                "missed": 0,
            }
        )

    # -- commands ----------------------------------------------------------------

    def create(
        self,
        artifact_id: str,
        label: str,
        steps: list[dict] | None = None,
        duration: float | None = None,
        notify_targets: list[str] | None = None,
        start: bool = False,
        tracks: list[dict] | None = None,
        advance: str | None = None,
        remind_every: float | None = None,
    ) -> dict:
        if len(self._timers) >= MAX_TIMERS:
            raise ArtifactError("limit", f"at most {MAX_TIMERS} timers")
        label = _clean_str(label, 120, "label")
        steps = steps or []
        if not isinstance(steps, list) or len(steps) > MAX_STEPS:
            raise ArtifactError("invalid", f"steps must be a list of at most {MAX_STEPS}")
        declared = _normalize_tracks(tracks)
        ids = [t["id"] for t in declared]

        def known(tid: str) -> None:
            if tid not in ids:
                if len(ids) >= MAX_TRACKS:
                    raise ArtifactError("invalid", f"at most {MAX_TRACKS} tracks")
                ids.append(tid)
                declared.append({"id": tid, "label": tid.replace("-", " ").replace("_", " ").title()})

        multi = bool(declared)
        prepared: list[tuple[dict, dict]] = []
        for raw in steps:
            if not isinstance(raw, dict):
                raise ArtifactError("invalid", "each step must be an object")
            track = raw.get("track")
            after = raw.get("after")
            join = track == JOIN_TRACK or after is not None
            if not join and track not in (None, ""):
                tid = str(track)
                if not TRACK_ID_RE.match(tid):
                    raise ArtifactError("invalid", "track ids use letters, digits, - and _ (max 32 characters)")
                known(tid)
                multi = True
            elif join:
                multi = True
            prepared.append((raw, {"join": join}))
        has_untracked = any(not info["join"] and raw.get("track") in (None, "") for raw, info in prepared)
        if has_untracked or not ids:
            known(DEFAULT_TRACK)

        normalized = []
        for raw, info in prepared:
            join = info["join"]
            auto_at = join and raw.get("at") is None
            try:
                at = float(raw["at"]) if "at" in raw and raw["at"] is not None else (0.0 if join else None)
                if at is None:
                    raise KeyError("at")
            except (KeyError, TypeError, ValueError) as err:
                raise ArtifactError("invalid", "each step needs a numeric 'at' (seconds)") from err
            if not 0 <= at <= MAX_SECONDS:
                raise ArtifactError("invalid", "step 'at' is out of range")
            step: dict[str, Any] = {
                "at": at,
                "plan_at": at,
                "label": _clean_str(raw.get("label"), 120, "step label"),
                "detail": str(raw.get("detail") or "")[:500],
                "notify": bool(raw.get("notify", True)),
            }
            if auto_at:
                step["_auto_at"] = True
            if join:
                after = raw.get("after")
                if after is None or after == JOIN_TRACK or after == [JOIN_TRACK]:
                    deps = list(ids)
                elif isinstance(after, list) and after and all(isinstance(a, str) for a in after):
                    deps = [str(a) for a in after]
                    for dep in deps:
                        if dep not in ids:
                            raise ArtifactError("invalid", f"'after' names unknown track '{dep}'")
                else:
                    raise ArtifactError("invalid", "'after' must be a list of track ids")
                step.update(track=JOIN_TRACK, after=deps)
            else:
                tid = str(raw.get("track") or DEFAULT_TRACK)
                step["track"] = tid
            if raw.get("reminder") or raw.get("kind") == "reminder":
                clash = [k for k in ("advance", "gate", "duration", "endLabel", "after", "remindEvery") if raw.get(k) is not None]
                if clash or join:
                    raise ArtifactError("invalid", f"a reminder only alerts on time; it can't have {', '.join(clash) or 'a join'}")
                anchor = raw.get("anchor") or "step"
                if anchor not in ("step", "timer"):
                    raise ArtifactError("invalid", 'anchor must be "step" or "timer"')
                step.update(reminder=True, anchor=anchor)
                normalized.append(step)
                continue
            if raw.get("advance") is not None:
                step["advance"] = _advance_mode(raw["advance"])
            if raw.get("remindEvery") is not None:
                step["remindEvery"] = _remind_seconds(raw["remindEvery"])
            if raw.get("gate"):
                step["gate"] = _normalize_gate(raw["gate"])
            elif raw.get("duration") is not None:
                try:
                    span = float(raw["duration"])
                except (TypeError, ValueError) as err:
                    raise ArtifactError("invalid", "step duration must be a number of seconds") from err
                if not 0 < span <= MAX_SECONDS:
                    raise ArtifactError("invalid", "step duration is out of range")
                step["duration"] = span
                if raw.get("endLabel"):
                    step["endLabel"] = _clean_str(raw["endLabel"], 120, "endLabel")
            normalized.append(step)
        plan_end = max((s["at"] + (s.get("duration") or 0) for s in normalized if "_auto_at" not in s and not s.get("reminder")), default=0.0)
        for step in normalized:
            if step.pop("_auto_at", False):
                step["at"] = step["plan_at"] = plan_end  # a join with no time sorts after the work it waits for
        normalized.sort(key=lambda s: s["at"])
        last = max((s["at"] + (s.get("duration") or 0) for s in normalized if not s.get("reminder")), default=0.0)
        try:
            total = float(duration) if duration is not None else last
        except (TypeError, ValueError) as err:
            raise ArtifactError("invalid", "duration must be a number of seconds") from err
        if total <= 0 or total > MAX_SECONDS:
            raise ArtifactError("invalid", "duration must be between 1 second and 7 days")
        if total < last - EPS:
            raise ArtifactError("invalid", "duration must not be shorter than the last step")
        targets = [str(t) for t in (notify_targets or [])][:10]

        timer_id = uuid.uuid4().hex[:8]
        timer = {
            "id": timer_id,
            "artifact_id": artifact_id,
            "label": label,
            "steps": normalized,
            "tracks": declared,
            "multi": multi,
            "duration": total,
            "planned_duration": total,
            "auto_duration": duration is None,
            "advance": _advance_mode(advance) if advance is not None else None,
            "remind_every": _remind_seconds(remind_every) if remind_every is not None else None,
            "orig_steps": copy.deepcopy(normalized),
            "orig_duration": total,
            "step_log": [],
            "notify_targets": targets,
            "state": "idle",
            "elapsed": 0.0,
            "started_at": None,
            "created": self._clock(),
            "finished_at": None,
            "started_wall": None,
            "history": [],
            "tstate": {},
        }
        self._init_tstate(timer)
        self._timers[timer_id] = timer
        self._save()
        self._emit_state(timer, "create")
        if start:
            return self.start(timer_id)
        return self.view(timer)

    @staticmethod
    def _restore(timer: dict) -> None:
        """Back to the schedule as created (extends and overtime shifts are undone)."""
        if timer.get("orig_steps"):
            timer["steps"] = copy.deepcopy(timer["orig_steps"])
            timer["duration"] = timer.get("orig_duration", timer["duration"])
        timer["step_log"] = []
        timer["extra"] = {}

    @staticmethod
    def _add_extra(timer: dict, tid: str | None, index: int | None, kind: str, seconds: float) -> None:
        """Remember time added to a step by a gate answer ("gate") or +/- minutes ("extend")."""
        if index is None or not seconds:
            return
        row = timer.setdefault("extra", {}).setdefault(
            str(index), {"index": index, "track": tid, "gate": 0.0, "extend": 0.0})
        row[kind] = round(row[kind] + seconds, 3)

    def _init_tstate(self, timer: dict) -> None:
        names = [t["id"] for t in timer["tracks"]]
        if any(s["track"] == JOIN_TRACK for s in timer["steps"]):
            names.append(JOIN_TRACK)
        timer["tstate"] = {name: self._new_tstate() for name in names}

    def start(self, timer_id: str, artifact_id: str | None = None, source: str | None = None) -> dict:
        timer = self._get(timer_id, artifact_id)
        if timer["state"] not in ("idle", "finished"):
            raise ArtifactError("invalid_state", f"timer is {timer['state']}; reset it first")
        self._restore(timer)
        self._init_tstate(timer)
        for tid, ts in timer["tstate"].items():
            if tid == JOIN_TRACK:
                continue
            for time_, _order, kind, _index in self._events(timer, tid):
                if time_ > EPS or kind != "start":
                    break
                ts["ev"] += 1  # offset-0 steps are visible on screen, not announced
        now = self._clock()
        timer.update(state="running", elapsed=0.0, started_at=now, finished_at=None, started_wall=now, history=[])
        self._log(timer, "start", now, source=source)
        self._save()
        self._arm(timer)
        self._emit_state(timer, "start")
        return self.view(timer)

    def pause(self, timer_id: str, artifact_id: str | None = None, track: str | None = None, source: str | None = None) -> dict:
        timer = self._get(timer_id, artifact_id)
        tid = self._track_arg(timer, track)
        now = self._clock()
        if tid is None:
            if timer["state"] != "running":
                raise ArtifactError("invalid_state", "timer is not running")
            timer["elapsed"] = self._elapsed(timer, now)
            timer.update(state="paused", started_at=None)
            self._disarm(timer_id)
            self._log(timer, "pause", now, source=source)
            self._save()
            self._emit_state(timer, "pause")
            return self.view(timer)
        if timer["state"] not in ("running", "paused"):
            raise ArtifactError("invalid_state", "timer is not active")
        ts = timer["tstate"][tid]
        if ts["hold_since"] is not None:
            raise ArtifactError("invalid_state", f"track '{tid}' is already paused")
        ts.update(hold_since=self._elapsed(timer, now), manual=True)
        self._log(timer, "pause", now, track=tid, source=source)
        self._save()
        self._arm(timer)
        self._emit_state(timer, "pause", track=tid)
        return self.view(timer)

    def resume(self, timer_id: str, artifact_id: str | None = None, track: str | None = None, source: str | None = None) -> dict:
        timer = self._get(timer_id, artifact_id)
        tid = self._track_arg(timer, track)
        now = self._clock()
        if tid is None:
            if timer["state"] != "paused":
                raise ArtifactError("invalid_state", "timer is not paused")
            timer.update(state="running", started_at=now)
            self._log(timer, "resume", now, source=source)
            self._save()
            self._arm(timer)
            self._emit_state(timer, "resume")
            return self.view(timer)
        ts = timer["tstate"][tid]
        if ts["waiting"] is not None:
            raise ArtifactError("invalid_state", f"track '{tid}' is waiting for an answer")
        if ts["hold_since"] is None:
            raise ArtifactError("invalid_state", f"track '{tid}' is not paused")
        self._release(ts, self._elapsed(timer, now))
        self._log(timer, "resume", now, track=tid, source=source)
        return self._settle(timer, now, "resume", track=tid)

    def reset(self, timer_id: str, artifact_id: str | None = None, source: str | None = None) -> dict:
        timer = self._get(timer_id, artifact_id)
        self._disarm(timer_id)
        timer.update(state="idle", elapsed=0.0, started_at=None, finished_at=None)
        self._restore(timer)
        self._init_tstate(timer)
        self._save()
        self._emit_state(timer, "reset")
        return self.view(timer)

    def skip(
        self,
        timer_id: str,
        to_index: int | None = None,
        artifact_id: str | None = None,
        track: str | None = None,
        source: str | None = None,
    ) -> dict:
        timer = self._get(timer_id, artifact_id)
        if timer["state"] not in ("running", "paused"):
            raise ArtifactError("invalid_state", "timer is not active")
        now = self._clock()
        big_t = self._elapsed(timer, now)
        steps = timer["steps"]
        tid = self._track_arg(timer, track)
        position = None
        if to_index is not None:
            if not isinstance(to_index, int) or isinstance(to_index, bool) or not 0 <= to_index < len(steps):
                raise ArtifactError("invalid", "to_index is out of range")
            if steps[to_index].get("reminder"):
                raise ArtifactError("invalid", "a reminder is not a step: Next skips past it")
            tid = steps[to_index]["track"]
            events = self._events(timer, tid)
            position = next((i for i, e in enumerate(events) if e[3] == to_index and e[2] != "end"), None)
        else:
            if tid is None:  # the track with the soonest upcoming event
                pick = self.view(timer)
                tid = next(
                    (v["id"] for v in [*pick["tracks"], pick["join"]] if v and v["next_index"] is not None
                     and v["next_index"] == pick["next_index"] and v["state"] != "waiting"),
                    None,
                )
                if tid is None and pick["waiting"]:
                    tid = pick["waiting"][0]["track"]
                if tid is None:  # nothing left to run: a lane still holding on its last manual step
                    tid = next((t for t in timer["tstate"] if t != JOIN_TRACK and self._tail_hold(timer, t)), None)
            if tid is not None:
                events = self._events(timer, tid)
                ev = timer["tstate"][tid]["ev"]
                position = ev
                hold = self._manual_hold(timer, tid, events, ev)
                if hold and hold["boundary"] >= len(events) and timer["tstate"][tid]["waiting"] is None:
                    return self._release_tail(timer, tid, hold, big_t, now, source)
                if hold:
                    position = hold["boundary"]  # past any reminders and end-of-activity alerts
                while position < len(events) and events[position][2] == "end":
                    position += 1  # "Next" moves on to the next step, past any end-of-activity alert
                if position >= len(events):
                    position = None
                if timer["tstate"][tid]["waiting"] is not None:
                    position = ev  # skipping a gate answers it
        if tid is None or position is None:
            if track is not None and len(timer["tstate"]) > 1:
                return self.view(timer)  # that lane has nothing left to skip
            self._log(timer, "skip", now, source=source)
            self._finish(timer, now)
            return self.view(timer)
        ts = timer["tstate"][tid]
        events = self._events(timer, tid)
        time_, _o, kind, index = events[position]
        step = steps[index]
        self._log_step(timer, tid, events, ts["ev"], time_, big_t, now)
        if tid != JOIN_TRACK and kind == "start" and not step.get("duration") and not step.get("gate") and step["at"] >= timer["duration"] - EPS:
            self._log(timer, "skip", now, track=tid if timer.get("multi") else None, index=index, source=source)
            self._finish(timer, now, index)
            return self.view(timer)
        ts["ot"] = None
        for number, other in enumerate(steps):  # reminders of the step being left will not fire
            if (other.get("reminder") and other["track"] == tid and other.get("anchor") != "timer"
                    and str(number) not in ts["rem"] and other["at"] < time_ - EPS):
                ts["rem"][str(number)] = {"state": "skipped", "at": None}
        if ts["hold_since"] is not None:
            if ts["waiting"] is not None:
                self._release(ts, big_t)
                ts["held"] = big_t - time_
            else:
                ts["held"] = ts["hold_since"] - time_
        else:
            ts["held"] = big_t - time_
        ts["waiting"], ts["auto_at"] = None, None
        # Landing on a check gate means asking it, so the gate event stays to be handled.
        asks = kind == "gate" or (kind == "start" and step.get("gate"))
        if asks:
            ts["rel"] = self._cur_step(timer, events, ts["ev"])  # the step Next ends no longer holds
        ts["ev"] = position if asks else position + 1
        self._log(timer, "skip", now, track=tid if timer.get("multi") else None, index=index, source=source)
        return self._settle(timer, now, "skip", track=tid if timer.get("multi") else None)

    def _log_step(self, timer: dict, tid: str, events: list, ev: int, time_: float, big_t: float, now: float) -> None:
        """Record planned vs actual for the (non-gate) step Next is ending."""
        steps = timer["steps"]
        cur = self._cur_step(timer, events, ev)
        if cur is None or tid == JOIN_TRACK or steps[cur].get("gate"):
            return
        began = steps[cur]["at"]
        planned = steps[cur]["duration"] if steps[cur].get("duration") else time_ - began
        timer.setdefault("step_log", []).append(
            {"index": cur, "track": tid, "label": steps[cur]["label"], "planned": round(planned, 3),
             "actual": round(max(0.0, self._tel(timer, tid, big_t) - began), 3), "t": now}
        )
        del timer["step_log"][:-MAX_TIMER_LOG]

    def _release_tail(self, timer: dict, tid: str, hold: dict, big_t: float, now: float, source: str | None) -> dict:
        """Next on the last manual step of a lane: it ends now, and the lane is done."""
        ts = timer["tstate"][tid]
        events = self._events(timer, tid)
        self._log_step(timer, tid, events, ts["ev"], hold["end"], big_t, now)
        ts["ot"], ts["rel"], ts["ev"] = None, hold["cur"], len(events)
        if ts["hold_since"] is not None:
            ts["held"] = ts["hold_since"] - hold["end"]
        else:
            ts["held"] = big_t - hold["end"]
        self._log(timer, "skip", now, track=tid if timer.get("multi") else None, index=hold["cur"], source=source)
        return self._settle(timer, now, "skip", track=tid if timer.get("multi") else None)

    def _shift_track(self, timer: dict, tid: str, seconds: float) -> None:
        ts = timer["tstate"][tid]
        el = self._tel(timer, tid, self._elapsed(timer, self._clock()))
        ot = ts.get("ot")
        if ot is not None:
            # Overtime: give the step more time by winding the lane clock back, so every later
            # step moves with it and earlier steps stay untouched.
            ts["held"] += seconds
            ot["since"] += seconds
            if ot.get("next_remind") is not None:
                ot["next_remind"] += seconds
            ts["offset"] += seconds
            hold = self._manual_hold(timer, tid, self._events(timer, tid), ts["ev"])
            if not hold or el - seconds < hold["end"] - EPS:
                ts["ot"] = None
            return
        for step in timer["steps"]:
            if step["track"] != tid or (step.get("reminder") and step.get("anchor") == "timer"):
                continue
            if step["at"] > el + EPS:
                step["at"] = max(el + 1.0, step["at"] + seconds)
            elif step.get("duration") and not step.get("gate") and step["at"] + step["duration"] > el + EPS:
                step["duration"] = max(el + 1.0 - step["at"], step["duration"] + seconds)
        for regate in ts["regates"]:
            if regate["at"] > el + EPS:
                regate["at"] = max(el + 1.0, regate["at"] + seconds)
        ts["offset"] += seconds

    def extend(
        self,
        timer_id: str,
        seconds: float,
        artifact_id: str | None = None,
        track: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Push the remaining steps (and the end) later (or earlier) by ``seconds``.

        With ``track`` only that lane moves; without it, every lane does.
        """
        timer = self._get(timer_id, artifact_id)
        if timer["state"] not in ("running", "paused"):
            raise ArtifactError("invalid_state", "timer is not active")
        tid = self._track_arg(timer, track)
        try:
            seconds = float(seconds)
        except (TypeError, ValueError) as err:
            raise ArtifactError("invalid", "seconds must be a number") from err
        if seconds == 0 or abs(seconds) > 3600:
            raise ArtifactError("invalid", "seconds must be non-zero and within one hour")
        now = self._clock()
        big_t = self._elapsed(timer, now)
        targets = [tid] if tid else list(timer["tstate"])
        for name in targets:
            self._shift_track(timer, name, seconds)
        self._fix_duration(timer, None if tid else seconds, big_t)
        focus = self._focus(timer, tid)
        self._add_extra(timer, tid, focus, "extend", seconds)
        self._log(timer, "extend", now, seconds=seconds, track=tid, index=focus, source=source)
        self._save()
        self._arm(timer)
        self._emit_state(timer, "extend", track=tid)
        return self.view(timer)

    def _focus(self, timer: dict, tid: str | None) -> int | None:
        view = self.view(timer)
        if tid:
            return next((t["current_index"] for t in view["tracks"] if t["id"] == tid), None)
        return view["current_index"] if view["current_index"] >= 0 else None

    def answer(
        self,
        timer_id: str,
        index: int,
        answer: Any,
        artifact_id: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Answer a check gate: "yes" (done) or a number of seconds to cook longer."""
        timer = self._get(timer_id, artifact_id)
        if timer["state"] not in ("running", "paused"):
            raise ArtifactError("invalid_state", "timer is not active")
        tid = next((t for t, ts in timer["tstate"].items() if ts["waiting"] is not None and ts["waiting"] == index), None)
        if tid is None:
            raise ArtifactError("invalid_state", f"step {index} is not waiting for an answer")
        seconds = None
        if not (answer is True or (isinstance(answer, str) and answer.strip().lower() in YES_WORDS)):
            try:
                seconds = float(answer)
            except (TypeError, ValueError) as err:
                raise ArtifactError("invalid", 'answer must be "yes" or a number of seconds') from err
            if not 0 < seconds <= 3600:
                raise ArtifactError("invalid", "extra time must be between 1 second and one hour")
        now = self._clock()
        big_t = self._elapsed(timer, now)
        ts = timer["tstate"][tid]
        el = self._tel(timer, tid, big_t)
        self._release(ts, big_t)
        if seconds:
            # Push the rest of this lane back and ask again when the extra time is up.
            for step in timer["steps"]:
                if step["track"] == tid and step["at"] > el + EPS:
                    step["at"] += seconds
            for regate in ts["regates"]:
                if regate["at"] > el + EPS:
                    regate["at"] += seconds
            if len(ts["regates"]) >= MAX_REGATES:
                raise ArtifactError("limit", "this check has been repeated too many times")
            ts["regates"].append({"index": index, "at": el + seconds})
            ts["offset"] += seconds
            self._fix_duration(timer)
            self._add_extra(timer, tid, index, "gate", seconds)
        self._log(timer, "answer", now, track=tid, index=index, answer="yes" if seconds is None else seconds, source=source)
        return self._settle(timer, now, "answer", track=tid, index=index)

    def delete(self, timer_id: str, artifact_id: str | None = None) -> None:
        timer = self._get(timer_id, artifact_id)
        self._disarm(timer_id)
        del self._timers[timer_id]
        self._save()
        self._emit(
            {
                "type": "timer",
                "change": "delete",
                "timer": {"id": timer_id, "artifact_id": timer["artifact_id"]},
                "server_now": self._clock(),
            }
        )

    def delete_for_artifact(self, artifact_id: str) -> None:
        for timer_id in [t["id"] for t in self._timers.values() if t["artifact_id"] == artifact_id]:
            self.delete(timer_id)

    def control(
        self,
        timer_id: str,
        action: str,
        *,
        artifact_id: str | None = None,
        to_index: int | None = None,
        seconds: float | None = None,
        track: str | None = None,
        answer: Any = None,
        source: str | None = None,
    ) -> dict | None:
        if action == "start":
            return self.start(timer_id, artifact_id, source)
        if action == "pause":
            return self.pause(timer_id, artifact_id, track, source)
        if action == "resume":
            return self.resume(timer_id, artifact_id, track, source)
        if action == "reset":
            return self.reset(timer_id, artifact_id, source)
        if action == "skip":
            return self.skip(timer_id, to_index, artifact_id, track, source)
        if action == "extend":
            return self.extend(timer_id, 60 if seconds is None else seconds, artifact_id, track, source)
        if action == "answer":
            if not isinstance(to_index, int) or isinstance(to_index, bool):
                raise ArtifactError("invalid", "answer needs the step index (to_index)")
            return self.answer(timer_id, to_index, "yes" if answer is None else answer, artifact_id, source)
        if action == "delete":
            self.delete(timer_id, artifact_id)
            return None
        raise ArtifactError("invalid", f"unknown timer action '{action}'")
