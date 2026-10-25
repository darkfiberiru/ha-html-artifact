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


def lint_html(html: str, caps_requested: Any = None) -> list[dict[str, str]]:
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
    ) -> dict[str, Any]:
        report = self._data.get(artifact_id)
        if report is None or report["version"] != version:
            report = {"version": version, "renders": 0, "settled": False, "sources": [], "errors": [], "updated": 0.0}
            self._data[artifact_id] = report
        if settled:
            report["renders"] += 1
            report["settled"] = True
        if source not in report["sources"]:
            report["sources"].append(source)
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

class TimerEngine(_Listeners):
    """Server-side, restart-safe timers with milestone steps.

    A timer has a total ``duration`` and ``steps``, each firing ``at`` an offset
    (seconds since start). A step at offset 0 is shown immediately and not
    announced. State is derived from ``elapsed`` (banked while paused) plus the
    time since ``started_at``, so every client sees the same timer.

    Events emitted to listeners:
      {"type": "timer", "change": ..., "timer": view, "server_now": ...}
      {"type": "timer_fired", "kind": "step" | "finished", "timer": view,
       "step": {...} | None, "index": int | None, "missed": int}
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

    # -- views -------------------------------------------------------------

    def _elapsed(self, timer: dict, now: float) -> float:
        elapsed = timer["elapsed"]
        if timer["state"] == "running":
            elapsed += now - timer["started_at"]
        return max(0.0, min(elapsed, timer["duration"]))

    def view(self, timer: dict) -> dict[str, Any]:
        now = self._clock()
        elapsed = self._elapsed(timer, now)
        steps = timer["steps"]
        current = -1
        for index, step in enumerate(steps):
            if step["at"] <= elapsed + EPS:
                current = index
        nxt = current + 1 if current + 1 < len(steps) else None
        next_at = steps[nxt]["at"] if nxt is not None else timer["duration"]
        next_in = max(0.0, next_at - elapsed)
        remaining = max(0.0, timer["duration"] - elapsed)
        running = timer["state"] == "running"
        return {
            "id": timer["id"],
            "artifact_id": timer["artifact_id"],
            "label": timer["label"],
            "state": timer["state"],
            "steps": copy.deepcopy(steps),
            "duration": timer["duration"],
            "notify_targets": list(timer.get("notify_targets") or []),
            "created": timer["created"],
            "elapsed_now": round(elapsed, 3),
            "remaining": round(remaining, 3),
            "current_index": current,
            "next_index": nxt,
            "next_in": round(next_in, 3),
            "next_due": now + next_in if running else None,
            "ends_at": now + remaining if running else None,
            "server_now": now,
        }

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

    def _emit_state(self, timer: dict, change: str) -> None:
        self._emit({"type": "timer", "change": change, "timer": self.view(timer), "server_now": self._clock()})

    # -- scheduling ----------------------------------------------------------

    def _disarm(self, timer_id: str) -> None:
        cancel = self._cancels.pop(timer_id, None)
        if cancel:
            cancel()

    def _next_target(self, timer: dict) -> float:
        steps, duration = timer["steps"], timer["duration"]
        index = timer["fired"]
        while index < len(steps):
            if steps[index]["at"] < duration - EPS:
                return steps[index]["at"]
            index += 1
        return duration

    def _arm(self, timer: dict) -> None:
        self._disarm(timer["id"])
        if timer["state"] != "running":
            return
        delay = max(0.0, self._next_target(timer) - self._elapsed(timer, self._clock()))
        timer_id = timer["id"]
        self._cancels[timer_id] = self._scheduler(delay, lambda: self._on_due(timer_id))

    def _on_due(self, timer_id: str) -> None:
        self._cancels.pop(timer_id, None)
        timer = self._timers.get(timer_id)
        if timer is None or timer["state"] != "running":
            return
        now = self._clock()
        elapsed = self._elapsed(timer, now)
        steps, duration = timer["steps"], timer["duration"]
        due: list[int] = []
        while (
            timer["fired"] < len(steps)
            and steps[timer["fired"]]["at"] < duration - EPS
            and steps[timer["fired"]]["at"] <= elapsed + EPS
        ):
            due.append(timer["fired"])
            timer["fired"] += 1
        if elapsed + EPS >= duration:
            self._finish(timer, now)
            return

        on_time = [i for i in due if steps[i]["at"] >= elapsed - LATE_AFTER]
        missed = len(due) - len(on_time)
        to_fire = on_time
        if due and not on_time:  # woke up long after: announce only the latest step
            to_fire = due[-1:]
            missed = len(due) - 1
        self._save()
        self._emit_state(timer, "step" if due else "tick")
        for n, index in enumerate(to_fire):
            if steps[index].get("notify", True):
                self._emit(
                    {
                        "type": "timer_fired",
                        "kind": "step",
                        "timer": self.view(timer),
                        "step": copy.deepcopy(steps[index]),
                        "index": index,
                        "missed": missed if n == 0 else 0,
                    }
                )
        self._arm(timer)

    def _finish(self, timer: dict, now: float) -> None:
        steps, duration = timer["steps"], timer["duration"]
        last = steps[-1] if steps and steps[-1]["at"] >= duration - EPS else None
        self._disarm(timer["id"])
        timer.update(state="finished", elapsed=duration, started_at=None, fired=len(steps), finished_at=now)
        self._save()
        self._emit_state(timer, "finish")
        self._emit(
            {
                "type": "timer_fired",
                "kind": "finished",
                "timer": self.view(timer),
                "step": copy.deepcopy(last),
                "index": len(steps) - 1 if last else None,
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
    ) -> dict:
        if len(self._timers) >= MAX_TIMERS:
            raise ArtifactError("limit", f"at most {MAX_TIMERS} timers")
        label = _clean_str(label, 120, "label")
        steps = steps or []
        if not isinstance(steps, list) or len(steps) > MAX_STEPS:
            raise ArtifactError("invalid", f"steps must be a list of at most {MAX_STEPS}")
        normalized = []
        for raw in steps:
            if not isinstance(raw, dict):
                raise ArtifactError("invalid", "each step must be an object")
            try:
                at = float(raw["at"])
            except (KeyError, TypeError, ValueError) as err:
                raise ArtifactError("invalid", "each step needs a numeric 'at' (seconds)") from err
            if not 0 <= at <= MAX_SECONDS:
                raise ArtifactError("invalid", "step 'at' is out of range")
            normalized.append(
                {
                    "at": at,
                    "label": _clean_str(raw.get("label"), 120, "step label"),
                    "detail": str(raw.get("detail") or "")[:500],
                    "notify": bool(raw.get("notify", True)),
                }
            )
        normalized.sort(key=lambda s: s["at"])
        last = normalized[-1]["at"] if normalized else 0.0
        try:
            total = float(duration) if duration is not None else last
        except (TypeError, ValueError) as err:
            raise ArtifactError("invalid", "duration must be a number of seconds") from err
        if total <= 0 or total > MAX_SECONDS:
            raise ArtifactError("invalid", "duration must be between 1 second and 7 days")
        if total < last:
            raise ArtifactError("invalid", "duration must not be shorter than the last step")
        targets = [str(t) for t in (notify_targets or [])][:10]

        timer_id = uuid.uuid4().hex[:8]
        timer = {
            "id": timer_id,
            "artifact_id": artifact_id,
            "label": label,
            "steps": normalized,
            "duration": total,
            "notify_targets": targets,
            "state": "idle",
            "elapsed": 0.0,
            "started_at": None,
            "fired": 0,
            "created": self._clock(),
            "finished_at": None,
        }
        self._timers[timer_id] = timer
        self._save()
        self._emit_state(timer, "create")
        if start:
            return self.start(timer_id)
        return self.view(timer)

    def start(self, timer_id: str, artifact_id: str | None = None) -> dict:
        timer = self._get(timer_id, artifact_id)
        if timer["state"] not in ("idle", "finished"):
            raise ArtifactError("invalid_state", f"timer is {timer['state']}; reset it first")
        fired = 0
        while fired < len(timer["steps"]) and timer["steps"][fired]["at"] <= EPS:
            fired += 1  # offset-0 steps are visible on screen, not announced
        timer.update(state="running", elapsed=0.0, started_at=self._clock(), fired=fired, finished_at=None)
        self._save()
        self._arm(timer)
        self._emit_state(timer, "start")
        return self.view(timer)

    def pause(self, timer_id: str, artifact_id: str | None = None) -> dict:
        timer = self._get(timer_id, artifact_id)
        if timer["state"] != "running":
            raise ArtifactError("invalid_state", "timer is not running")
        timer["elapsed"] = self._elapsed(timer, self._clock())
        timer.update(state="paused", started_at=None)
        self._disarm(timer_id)
        self._save()
        self._emit_state(timer, "pause")
        return self.view(timer)

    def resume(self, timer_id: str, artifact_id: str | None = None) -> dict:
        timer = self._get(timer_id, artifact_id)
        if timer["state"] != "paused":
            raise ArtifactError("invalid_state", "timer is not paused")
        timer.update(state="running", started_at=self._clock())
        self._save()
        self._arm(timer)
        self._emit_state(timer, "resume")
        return self.view(timer)

    def reset(self, timer_id: str, artifact_id: str | None = None) -> dict:
        timer = self._get(timer_id, artifact_id)
        self._disarm(timer_id)
        timer.update(state="idle", elapsed=0.0, started_at=None, fired=0, finished_at=None)
        self._save()
        self._emit_state(timer, "reset")
        return self.view(timer)

    def skip(self, timer_id: str, to_index: int | None = None, artifact_id: str | None = None) -> dict:
        timer = self._get(timer_id, artifact_id)
        if timer["state"] not in ("running", "paused"):
            raise ArtifactError("invalid_state", "timer is not active")
        now = self._clock()
        elapsed = self._elapsed(timer, now)
        steps = timer["steps"]
        if to_index is None:
            index = next((i for i, s in enumerate(steps) if s["at"] > elapsed + EPS), None)
        else:
            if not isinstance(to_index, int) or not 0 <= to_index < len(steps):
                raise ArtifactError("invalid", "to_index is out of range")
            index = to_index
        if index is None or steps[index]["at"] >= timer["duration"] - EPS:
            self._finish(timer, now)
            return self.view(timer)
        timer["elapsed"] = steps[index]["at"]
        if timer["state"] == "running":
            timer["started_at"] = now
        timer["fired"] = index + 1
        self._save()
        self._arm(timer)
        self._emit_state(timer, "skip")
        return self.view(timer)

    def extend(self, timer_id: str, seconds: float, artifact_id: str | None = None) -> dict:
        """Push every remaining step (and the end) later (or earlier) by ``seconds``."""
        timer = self._get(timer_id, artifact_id)
        if timer["state"] not in ("running", "paused"):
            raise ArtifactError("invalid_state", "timer is not active")
        try:
            seconds = float(seconds)
        except (TypeError, ValueError) as err:
            raise ArtifactError("invalid", "seconds must be a number") from err
        if seconds == 0 or abs(seconds) > 3600:
            raise ArtifactError("invalid", "seconds must be non-zero and within one hour")
        elapsed = self._elapsed(timer, self._clock())
        for step in timer["steps"]:
            if step["at"] > elapsed + EPS:
                step["at"] = max(elapsed + 1.0, step["at"] + seconds)
        timer["duration"] = max(elapsed + 1.0, timer["duration"] + seconds)
        self._save()
        self._arm(timer)
        self._emit_state(timer, "extend")
        return self.view(timer)

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
    ) -> dict | None:
        if action == "start":
            return self.start(timer_id, artifact_id)
        if action == "pause":
            return self.pause(timer_id, artifact_id)
        if action == "resume":
            return self.resume(timer_id, artifact_id)
        if action == "reset":
            return self.reset(timer_id, artifact_id)
        if action == "skip":
            return self.skip(timer_id, to_index, artifact_id)
        if action == "extend":
            return self.extend(timer_id, 60 if seconds is None else seconds, artifact_id)
        if action == "delete":
            self.delete(timer_id, artifact_id)
            return None
        raise ArtifactError("invalid", f"unknown timer action '{action}'")
