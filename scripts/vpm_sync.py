#!/usr/bin/env python3
"""Start the manager and safely receive completed video-generator handoffs.

Video-generation skills write a small handoff JSON in a shared inbox. This
command is the bridge used by the resource-management skill: it starts/probes
the one local manager runtime, reads only terminal handoff records, and passes
the public parts through the generic handoff adapter.

No task JSON is copied into a project.  A completed local or cacheable remote result without an
explicit project is given a deterministic new project on first intake; invalid
or incomplete mappings are kept in a small dot-file at the manager root for a
later retry.  The dot-file is blocked by the web server and is not included in
project ZIP exports.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable as IterableABC, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# Keep imports usable when this file is run directly from the installed skill.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from handoff_core import (  # type: ignore[import-not-found]  # noqa: E402
        ABSOLUTE_RE,
        PRIVATE_RE,
        SLUG_RE,
        TASK_ID_RE,
        CLIP_ID_RE,
        VIDEO_EXTS as HANDOFF_VIDEO_EXTS,
        clean_public,
        generation_completion_confirmed,
        handoff,
        local_video,
        remote_video_reference,
        approved_local_file,
        iter_video_references,
    )
except Exception:  # pragma: no cover - compatibility with old installations
    from handoff import (  # noqa: E402
        ABSOLUTE_RE,
        PRIVATE_RE,
        SLUG_RE,
        TASK_ID_RE,
        CLIP_ID_RE,
        VIDEO_EXTS as HANDOFF_VIDEO_EXTS,
        clean_public,
        generation_completion_confirmed,
        handoff,
        local_video,
        remote_video_reference,
        approved_local_file,
        iter_video_references,
    )
from vpm_record import P, empty_project, ensure_dirs, jwrite, sync_index  # noqa: E402


DEFAULT_PORT = 4200
DEFAULT_BIND = "0.0.0.0"
VIDEO_EXTS = set(HANDOFF_VIDEO_EXTS)
DEFAULT_TASK_DIR = Path(tempfile.gettempdir()) / "video-generator-handoffs"
PENDING_NAME = ".handoff_pending.json"
# A few video runners only leave a completed local file behind.  The manager
# can opt-in to a bounded fallback scan for those files, while still preferring
# the normal JSON handoff inbox.  These names are intentionally explicit: the
# fallback never walks an arbitrary workspace or home directory.
OUTPUT_ENV_KEYS = (
    "VIDEO_GENERATOR_OUTPUT_DIR",
    "VIDEO_GENERATOR_OUTPUT_ROOT",
    "VAM_OUTPUT_DIR",
    "CAPAFY_OUTPUT_DIR",
    "CAPAFY_AGENT_OUTPUTS_DIR",
)
OUTPUT_CHILD_NAMES = (
    "agent-outputs",
    "outputs",
    "video-generator-outputs",
)
OUTPUT_WATCH_STATE_NAME = ".output_watch_state.json"
MAX_OUTPUT_DIRS = 16
MAX_OUTPUT_STATE = 2000
# A manager window normally has one watcher, but a stale launcher or two
# host processes can briefly run scanners at the same time.  Keep the receive
# side serialized across processes as well as within one server process.  The
# lock is a short-lived implementation detail at the manager root and is
# never served or copied into a project ZIP.
SCAN_LOCK_NAME = ".handoff_scan.lock"
SCAN_LOCK_TIMEOUT = 20.0
SCAN_LOCK_STALE_AFTER = 180.0
MAX_TASK_BYTES = 8 * 1024 * 1024

# A pending handoff is a manager-queue state, not a video-generation failure.
# Keep a small, explicit classification so the UI can explain why a result is
# waiting without interpreting the upstream task's ``status=failed`` as a
# second generation attempt.  Older queue entries do not have ``category``;
# ``pending_category`` below derives the same value from their reason.
UNMATCHED_REASON_MARKERS = (
    "当前项目未选择", "当前项目不存在", "当前项目不可用", "目标项目不存在",
    "指定的项目", "任务映射", "项目映射", "项目 slug", "项目无效",
)


def pending_category(reason: object, explicit: object = None) -> str:
    """Classify a queued handoff for customer-facing aggregate counts."""

    value = str(explicit or "").strip().lower()
    if value in {"unmatched", "unmatched_project", "project_mapping"}:
        return "unmatched_project"
    if value in {"delivery", "delivery_failed", "handoff_failed"}:
        return "delivery_failed"
    text = str(reason or "").strip().lower()
    if any(marker.lower() in text for marker in UNMATCHED_REASON_MARKERS):
        return "unmatched_project"
    return "delivery_failed"
try:
    from vpm_privacy import (  # type: ignore[import-not-found]
        manager_child_environment,
        sanitized_process_environment,
    )
except Exception:  # pragma: no cover - old copied bundle fallback
    def manager_child_environment() -> dict[str, str]:
        child_env = os.environ.copy()
        for name in list(child_env):
            if re.search(
                r"(?:API[_-]?KEY|ACCESS[_-]?KEY|SECRET|TOKEN|PASSWORD|AUTH|CREDENTIAL|PRIVATE[_-]?KEY|SIGNATURE)",
                str(name), re.I,
            ):
                child_env.pop(name, None)
        return child_env

    @contextlib.contextmanager
    def sanitized_process_environment() -> Iterable[None]:
        original = dict(os.environ)
        safe = manager_child_environment()
        try:
            os.environ.clear()
            os.environ.update(safe)
            yield
        finally:
            os.environ.clear()
            os.environ.update(original)

TERMINAL_STATUSES = {
    "complete",
    "completed",
    "success",
    "succeeded",
    "failed",
    "error",
    "cancelled",
    "canceled",
    "timed_out",
    "timeout",
    # A handoff may intentionally normalize a terminal generation result with
    # an unavailable delivery file to ``partial``.  It still needs one scan so
    # the project task summary/state can be recorded and then becomes
    # idempotent; it is never re-submitted for generation.
    "partial",
}

# Older generators may have no project argument. These aliases allow a future
# handoff producer (or a user-created task fixture) to provide an explicit,
# safe mapping without coupling this skill to the generator's implementation.
PROJECT_KEYS = (
    "project_slug",
    "manager_project",
    "video_project",
    "capafy_project_slug",
    "project",
)
CLIP_KEYS = ("clip_id", "manager_clip_id", "clip", "segment_id")
# A preflight may reserve a manager task before the generator allocates its
# own provider task id.  These aliases are intentionally public/stable and are
# used only to look up an existing preparation map; they never become a
# provider-specific routing contract.
PREPARED_ID_KEYS = (
    "manager_task_id", "preflight_task_id", "prepared_task_id",
    "manager_request_id", "request_id",
)


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def stable_marker(prefix: str, *parts: Any) -> str:
    raw = "\x1f".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def resolve_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None and str(explicit).strip():
        return Path(os.path.abspath(os.path.expanduser(str(explicit))))
    for variable in ("VIDEO_ASSET_MANAGER_ROOT", "VPM_ROOT"):
        value = os.environ.get(variable)
        if value and value.strip():
            return Path(os.path.abspath(os.path.expanduser(value.strip())))
    # Capafy instances keep /home/user/workspace across resets, so prefer the team's
    # agreed projects path when it exists, or create it when its persistent parent
    # does.  Kept in lock-step with server.py so every entry point agrees.
    candidate = "/home/user/workspace/projects"
    parent = os.path.dirname(candidate)
    if os.path.isdir(candidate):
        return Path(candidate)
    if os.path.isdir(parent):
        try:
            os.makedirs(candidate, exist_ok=True)
            return Path(candidate)
        except OSError:
            pass
    workspace = os.environ.get("CAPAFY_WORKSPACE")
    if workspace and workspace.strip():
        return Path(os.path.abspath(os.path.join(
            os.path.expanduser(workspace.strip()), ".capafy", "video-asset-manager"
        )))
    return Path.home() / "workspace" / ".capafy" / "video-asset-manager"


def resolve_task_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit is not None and str(explicit).strip():
        return Path(os.path.abspath(os.path.expanduser(str(explicit))))
    for variable in ("VIDEO_GENERATOR_HANDOFF_DIR", "VIDEO_GENERATOR_TASK_DIR",
                     "VAM_HANDOFF_DIR"):
        value = os.environ.get(variable)
        if value and value.strip():
            return Path(os.path.abspath(os.path.expanduser(value.strip())))
    manager_root = os.environ.get("VIDEO_ASSET_MANAGER_ROOT") or os.environ.get("VPM_ROOT")
    if manager_root and manager_root.strip():
        candidate = Path(os.path.abspath(os.path.expanduser(manager_root.strip()))) / "inbox"
        if candidate.is_dir() and not candidate.is_symlink():
            return candidate
    # Hosted generator runs may place the terminal task beside the mounted
    # workspace instead of the system temp directory.  Prefer an existing
    # shared inbox so opening the manager receives the result in one step.
    workspace = os.environ.get("CAPAFY_WORKSPACE")
    if workspace and workspace.strip():
        base = Path(os.path.abspath(os.path.expanduser(workspace.strip())))
        for candidate in (base / ".capafy" / "video-generator-handoffs",
                          base / ".capafy" / "video-asset-manager" / "inbox",
                          base / "video-generator-handoffs"):
            if candidate.is_dir() and not candidate.is_symlink():
                return candidate
    for candidate in (Path.cwd() / "video-generator-handoffs",
                      Path.cwd() / "outputs" / "tasks"):
        if candidate.is_dir() and not candidate.is_symlink():
            return candidate
    return DEFAULT_TASK_DIR


def _split_configured_paths(value: object) -> list[str]:
    """Split an environment/CLI path list without interpreting file content."""

    if value is None:
        return []
    if isinstance(value, Mapping):
        # A mapping is not a path list.  In particular, do not stringify a
        # task/config object into a directory name or iterate its keys.
        return []
    # Public callers may pass the already-parsed list returned by a CLI or a
    # generator adapter.  Do not stringify that container into one impossible
    # path (and do not iterate mappings, whose keys are not path lists).
    if (isinstance(value, IterableABC)
            and not isinstance(value, (str, bytes, bytearray, os.PathLike, Mapping))):
        pieces: list[str] = []
        try:
            for item in value:
                pieces.extend(_split_configured_paths(item))
        except (TypeError, ValueError, OSError, RuntimeError):
            # Keep any paths yielded before a user-provided iterator failed;
            # a malformed iterator must not abort a manager scan.
            pass
        return pieces
    if isinstance(value, bytes):
        try:
            value = os.fsdecode(value)
        except (TypeError, ValueError):
            return []
    if isinstance(value, os.PathLike):
        try:
            value = os.fspath(value)
        except (TypeError, ValueError, OSError):
            return []
    if not isinstance(value, str):
        # Do not coerce arbitrary objects (for example an integer or a task
        # descriptor) into a filesystem path.  Only strings, bytes, and
        # path-like values are valid scalar directory specifications.
        return []
    text = str(value).strip()
    if not text:
        return []
    # ``os.pathsep`` is ``;`` on Windows and ``:`` on POSIX.  Do not split a
    # Windows drive letter when a POSIX host is inspecting a copied config.
    pieces = text.split(os.pathsep)
    if os.pathsep == ":" and re.match(r"^[A-Za-z]:[\\/].*", text):
        pieces = [text]
    return [piece.strip().strip('"') for piece in pieces if piece.strip()]


def _usable_output_dir(path: Path, manager_root: Path | None = None) -> bool:
    """Return whether a configured output directory is safe to inspect.

    The watcher only reads direct children of this directory.  Still reject
    roots, symlinks, and the manager root itself so a typo cannot turn the
    fallback into a whole-disk/workspace crawler.
    """

    try:
        if path.is_symlink():
            return False
        resolved = path.resolve(strict=False)
        if resolved == resolved.parent:
            return False
        if manager_root is not None:
            root = manager_root.resolve(strict=False)
            if resolved == root:
                return False
            # A directory containing the manager root is too broad for the
            # implicit fallback (for example the workspace directory that holds
            # the manager root).
            try:
                if os.path.commonpath((str(root), str(resolved))) == str(resolved):
                    return False
            except ValueError:
                return False
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def resolve_output_dirs(
    explicit: object = None,
    *,
    manager_root: Path | None = None,
) -> list[Path]:
    """Resolve bounded local-output directories for the file-only fallback.

    Explicit arguments and environment variables may name a directory that is
    not created yet; this is useful when the manager starts before a generator
    creates ``agent-outputs``.  Conventional fallback locations are included
    only when they already exist.  No recursive workspace scan is performed.
    """

    root = Path(manager_root).expanduser().resolve(strict=False) if manager_root else None
    requested: list[tuple[str, bool]] = []

    for value in _split_configured_paths(explicit):
        requested.append((value, True))

    for variable in OUTPUT_ENV_KEYS:
        raw = os.environ.get(variable)
        for value in _split_configured_paths(raw):
            requested.append((value, True))

    # A Capafy workspace variable identifies a workspace, not an output folder.
    # Add only its conventional children, never the workspace itself.
    workspace = os.environ.get("CAPAFY_WORKSPACE")
    for value in _split_configured_paths(workspace):
        try:
            base = Path(value).expanduser()
        except (TypeError, ValueError, OSError):
            continue
        for child in OUTPUT_CHILD_NAMES:
            requested.append((str(base / child), True))
        requested.extend((
            (str(base / ".capafy" / "video-generator-outputs"), True),
            (str(base / ".capafy" / "video-generator-outputs"), True),
        ))

    # Standard local/Capafy output buckets.  They are deliberately direct
    # children only and are considered implicit (existing-only) candidates.
    conventional_bases = [Path(tempfile.gettempdir()), Path.home() / "workspace"]
    for base in conventional_bases:
        for child in OUTPUT_CHILD_NAMES:
            requested.append((str(base / child), False))
        requested.extend((
            (str(base / ".capafy" / "video-generator-outputs"), False),
            (str(base / ".capafy" / "video-generator-outputs"), False),
        ))

    result: list[Path] = []
    seen: set[str] = set()
    for raw, explicit_value in requested:
        try:
            candidate = Path(raw).expanduser()
            if not explicit_value and not candidate.exists():
                continue
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if not _usable_output_dir(resolved, root):
            continue
        # Do not add the manager's private inbox: it is scanned as a task
        # directory and including it here would duplicate every handoff.
        if root is not None:
            try:
                if resolved == (root / "inbox").resolve(strict=False):
                    continue
            except (OSError, RuntimeError):
                pass
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        result.append(resolved)
        if len(result) >= MAX_OUTPUT_DIRS:
            break
    return result


def safe_slug(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or not SLUG_RE.fullmatch(text) or PRIVATE_RE.search(text) or ABSOLUTE_RE.search(text):
        return None
    if text.lower() in {"webapp", "trash", "api", "staging"}:
        return None
    return text


def safe_clip(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or not CLIP_ID_RE.fullmatch(text) or PRIVATE_RE.search(text) or ABSOLUTE_RE.search(text):
        return None
    return text


def safe_task_id(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if text and TASK_ID_RE.fullmatch(text) and not PRIVATE_RE.search(text) and not ABSOLUTE_RE.search(text):
        return text
    # Never persist an unsafe task id or the source filename.  A deterministic
    # marker still lets repeated scans remain idempotent.
    return stable_marker("task", fallback)


def read_active_project(root: Path) -> tuple[str | None, str | None]:
    marker = root / "active_project"
    try:
        if marker.is_symlink() or not marker.is_file():
            return None, "当前项目未选择。"
        text = marker.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None, "当前项目未选择。"
    slug = safe_slug(text)
    if not slug:
        return None, "当前项目未选择。"
    project_dir = root / slug
    project_file = project_dir / "project.json"
    try:
        root_real = root.resolve(strict=False)
        project_real = project_dir.resolve(strict=False)
        if (project_dir.is_symlink() or not project_dir.is_dir()
                or os.path.commonpath((str(root_real), str(project_real))) != str(root_real)
                or project_file.is_symlink() or not project_file.is_file()):
            return None, "当前项目不存在。"
    except OSError:
        return None, "当前项目不可用。"
    return slug, None


def task_status(task: dict[str, Any]) -> str:
    value = task.get("status", task.get("task_status"))
    return str(value or "").strip().lower()


def task_id_for(task: dict[str, Any], path: Path) -> str:
    # Keep the generator's explicit task id as the primary identity.  When a
    # generic result only carries the id returned by the manager preflight,
    # accept the stable aliases as a fallback so the preparation map can still
    # route it to its reserved project/clip.
    value = task.get("task_id", task.get("id"))
    if not value:
        for key in PREPARED_ID_KEYS:
            candidate = task.get(key)
            if candidate not in (None, ""):
                value = candidate
                break
    return safe_task_id(value, path.stem)


def _field_value(value: Any, keys: Iterable[str]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value and value[key] is not None:
                return value[key]
        # A nested project descriptor commonly uses slug/id.
        for key in ("slug", "project_slug", "id"):
            if key in value and value[key] is not None:
                return value[key]
    return value


def explicit_project(task: dict[str, Any]) -> tuple[str | None, bool]:
    """Return (slug, specified) while rejecting conflicting/unsafe fields."""
    values: list[str] = []
    specified = False
    for key in PROJECT_KEYS:
        if key not in task or task.get(key) is None:
            continue
        specified = True
        candidate = safe_slug(_field_value(task.get(key), ("slug", "project_slug", "id")))
        if candidate is None:
            return None, True
        values.append(candidate)
    if not values:
        return None, specified
    if len(set(values)) != 1:
        return None, True
    return values[0], True


def explicit_clip(task: dict[str, Any]) -> tuple[str | None, bool]:
    values: list[str] = []
    specified = False
    for key in CLIP_KEYS:
        if key not in task or task.get(key) is None:
            continue
        specified = True
        candidate = safe_clip(_field_value(task.get(key), ("id", "clip_id")))
        if candidate is None:
            return None, True
        values.append(candidate)
    if not values:
        return None, specified
    if len(set(values)) != 1:
        return None, True
    return values[0], True


def task_file_digest(path: Path) -> str | None:
    """Hash only enough bytes to identify a task fixture, never persist it."""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def load_task(path: Path) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Read a bounded, non-symlink JSON task.

    The returned third value is a private in-memory digest used only for
    diagnostics/idempotency markers; it is never written to project files.
    """
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TASK_BYTES:
            return None, "任务文件不可用。", None
        raw = path.read_bytes()
        marker = hashlib.sha256(raw).hexdigest()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "任务文件不可用。", None
    if not isinstance(value, dict):
        return None, "任务文件不可用。", marker
    return value, None, marker


def load_mapping(path: Path | None) -> dict[str, Any]:
    """Load an optional task->project map, retaining only object values."""
    if path is None:
        return {}
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    nested = value.get("tasks")
    if isinstance(nested, dict):
        value = nested
    return {str(key): item for key, item in value.items() if isinstance(item, (str, dict))}


def _task_mapping_identifiers(task: dict[str, Any] | None,
                              task_id: str, file_stem: str) -> list[str]:
    """Return stable identifiers that may refer to a preflight reservation."""

    values: list[str] = []
    for value in (task_id, file_stem):
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)
    if isinstance(task, dict):
        for key in PREPARED_ID_KEYS:
            value = task.get(key)
            if isinstance(value, dict):
                value = _field_value(value, ("id", key, "task_id", "request_id"))
            text = str(value or "").strip()
            if text and text not in values:
                values.append(text)
    return values


def mapping_values(mapping: dict[str, Any], task_id: str, file_stem: str,
                   task: dict[str, Any] | None = None) -> tuple[str | None, str | None, bool]:
    """Resolve a preparation map entry by task id or stable preflight alias.

    Direct task-id/file-stem lookup remains first.  The secondary pass lets a
    generator return a provider task id while carrying the manager's
    ``request_id``/``manager_task_id`` from preflight.  Only map values that
    explicitly contain one of those stable identifiers are considered.
    """

    identifiers = _task_mapping_identifiers(task, task_id, file_stem)
    item = None
    for identifier in identifiers:
        if identifier in mapping:
            item = mapping.get(identifier)
            break
    if item is None and isinstance(task, dict):
        # vpm_prepare stores request_id/task_id inside each value.  Scan the
        # bounded map once to support a result whose provider task id differs
        # from the prepared manager id.
        wanted = set(identifiers)
        for candidate in mapping.values():
            if not isinstance(candidate, dict):
                continue
            candidate_ids: set[str] = set()
            for key in ("task_id", "manager_task_id", "preflight_task_id",
                        "prepared_task_id", "request_id", "manager_request_id"):
                value = candidate.get(key)
                if isinstance(value, dict):
                    value = _field_value(value, ("id", "task_id", "request_id"))
                text = str(value or "").strip()
                if text:
                    candidate_ids.add(text)
            if candidate_ids & wanted:
                item = candidate
                break
    if item is None:
        return None, None, False
    if isinstance(item, str):
        slug = safe_slug(item)
        return slug, None, True
    if not isinstance(item, dict):
        return None, None, True
    raw_slug = item.get("project_slug", item.get("project", item.get("slug")))
    raw_clip = item.get("clip_id", item.get("clip"))
    # A mapping file is explicit user input: invalid values are a pending
    # mapping, never a reason to fall back to another project.
    slug = safe_slug(_field_value(raw_slug, ("slug", "project_slug", "id")))
    clip = safe_clip(_field_value(raw_clip, ("id", "clip_id"))) if raw_clip is not None else None
    if raw_slug is not None and slug is None:
        return None, None, True
    if raw_clip is not None and clip is None:
        return None, None, True
    return slug, clip, True


def pending_path(root: Path) -> Path:
    return root / PENDING_NAME


def read_pending(root: Path) -> dict[str, dict[str, Any]]:
    path = pending_path(root)
    try:
        if path.is_symlink() or not path.is_file():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    items = value.get("items") if isinstance(value, dict) else None
    if not isinstance(items, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        task_id = safe_task_id(item.get("task_id"), "pending")
        result[task_id] = {
            "task_id": task_id,
            "status": str(item.get("status") or "unknown")[:32],
            "reason": str(item.get("reason") or "等待项目匹配。")[:240],
            "category": pending_category(item.get("reason"), item.get("category")),
            "seen": str(item.get("seen") or now())[:40],
        }
    return result


def write_pending(root: Path, items: dict[str, dict[str, Any]]) -> None:
    path = pending_path(root)
    if not items:
        try:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        except OSError:
            pass
        return
    root.mkdir(parents=True, exist_ok=True)
    clean = sorted(items.values(), key=lambda item: str(item.get("task_id") or ""))
    # jwrite performs an atomic replace and writes only the sanitized shape.
    jwrite(path, {"schema": 1, "updated": now(), "items": clean})


def pending_add(items: dict[str, dict[str, Any]], task_id: str, status: str, reason: str) -> None:
    items[task_id] = {
        "task_id": task_id,
        "status": status[:32],
        "reason": reason[:240],
        "category": pending_category(reason),
        "seen": now(),
    }


def _prune_stale_pending(root: Path, task_dir: Path) -> None:
    """Drop pending queue entries whose source envelopes no longer exist.

    Pending rows are durable manager state, but a generator may remove or
    rotate its terminal envelope after delivery.  Keeping such rows forever
    makes the UI report work that can never be retried.  Require a complete
    inventory and a 24-hour age before pruning so an atomic rename or a short
    handoff race cannot lose a newly written result.
    """

    pending = read_pending(root)
    if not pending:
        return
    try:
        visible_ids: set[str] = set()
        if task_dir.is_dir() and not task_dir.is_symlink():
            for entry in task_dir.iterdir():
                if not (entry.is_file() and not entry.is_symlink()
                        and entry.suffix.lower() == ".json"
                        and not entry.name.startswith(".")):
                    continue
                try:
                    task, _load_error, _private_digest = load_task(entry)
                except Exception:
                    continue
                if task is None:
                    continue
                visible_ids.add(task_id_for(task, entry))
        stale_cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=24)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return

    changed = False
    for task_id in list(pending):
        seen = str(pending[task_id].get("seen") or "")[:19]
        if task_id not in visible_ids and seen and seen <= stale_cutoff:
            pending.pop(task_id, None)
            changed = True
    if changed:
        write_pending(root, pending)


@contextlib.contextmanager
def scan_lock(root: Path, *, timeout: float = SCAN_LOCK_TIMEOUT) -> Iterable[bool]:
    """Serialize handoff scans across manager processes.

    The web runtime starts a watcher in each process.  A duplicated/stale
    launcher must not let two scans read the same task before either one has
    written its idempotency summary.  ``O_EXCL`` gives us a portable Windows
    and POSIX claim without adding a dependency.  A very old lock is treated
    as abandoned so a crashed scanner cannot block future deliveries.
    """

    root = Path(root).absolute()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / SCAN_LOCK_NAME
    deadline = time.monotonic() + max(0.0, float(timeout))
    claimed = False
    while not claimed:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"pid={os.getpid()}\n".encode("ascii", "replace"))
            finally:
                os.close(fd)
            claimed = True
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > SCAN_LOCK_STALE_AFTER:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        except OSError:
            # A read-only/unsupported root should not make the manager crash;
            # the caller will return a compact "scan in progress" result.
            break
    try:
        yield claimed
    finally:
        if claimed:
            try:
                lock_path.unlink()
            except OSError:
                pass


def _task_candidate_key(path: Path, task: dict[str, Any]) -> tuple[int, int, int, str]:
    """Rank duplicate JSON envelopes for one stable task ID.

    Hosts occasionally leave a legacy envelope beside a richer handoff (for
    example, an older file has the video while the newer one also carries the
    original public script).  Process exactly one envelope: terminal state is
    preferred, then the amount of public result data, then the newest atomic
    file.  This keeps a richer public handoff (video + original script) even
    if a legacy video-only envelope was touched later.  The filename is a
    deterministic final tie-breaker.
    """

    try:
        mtime = int(path.stat().st_mtime_ns)
    except OSError:
        mtime = 0
    terminal = 1 if task_status(task) in TERMINAL_STATUSES else 0
    public_fields = 0
    for keys in (
        ("video_file", "video_path", "local_video", "video_url", "download_url", "object_url"),
        ("input_script", "public_script", "public_input", "input_script_file", "public_script_file", "script_file"),
        ("report", "report_file", "analysis_report"),
        ("project_slug", "project", "project_id"),
        ("clip_id", "clip", "segment_id"),
    ):
        if any(task.get(key) not in (None, "") for key in keys):
            public_fields += 1
    return terminal, public_fields, mtime, path.name.casefold()


def project_exists(root: Path, slug: str | None) -> bool:
    if not slug:
        return False
    project_dir = root / slug
    project_file = project_dir / "project.json"
    try:
        return (
            project_dir.is_dir()
            and not project_dir.is_symlink()
            and project_file.is_file()
            and not project_file.is_symlink()
        )
    except OSError:
        return False


def _auto_project_slug(task_id: str, task: dict[str, Any] | None = None) -> str:
    """Return a deterministic, generator-neutral slug for video-first intake.

    The producer label is deliberately excluded.  A provider/generator name
    can be sensitive, can contain credential-adjacent text, and is not needed
    to identify a project.  Task IDs are already normalized by the scanner;
    hashing them keeps the folder portable and collision-resistant while
    making repeated scans resolve to the same project.
    """
    _ = task  # retained for source compatibility; never used in the slug
    digest = hashlib.sha256(str(task_id or "").encode("utf-8")).hexdigest()[:16]
    return f"video-{digest}"


def _auto_project_title(task: dict[str, Any], task_id: str) -> str:
    """Pick only a short public label; never persist the task prompt."""
    for key in ("project_title", "title", "name", "summary", "message", "user_summary"):
        value = task.get(key)
        if isinstance(value, str):
            label = clean_public(value, limit=72)
            if label:
                return label
    return f"视频项目 {task_id[-12:]}"


def task_has_video_output(task: dict[str, Any], task_file: Path) -> bool:
    """Return whether a terminal task has a local file or a cacheable URL."""

    try:
        video, _error = local_video(task, task_file)
    except Exception:
        video = None
    return video is not None or bool(remote_video_reference(task))


def _task_video_reference_paths(task_dir: Path) -> set[str]:
    """Collect in-memory paths for videos referenced by terminal task JSON.

    The normal task inbox and the optional bare-output directories are often
    different folders.  ``vpm_receive._watch_inputs`` can suppress a video
    referenced by JSON in the *same* folder, but it cannot see references in
    the task inbox.  Gather those references before the output fallback runs
    so one generator result is not imported twice.  Paths stay in memory only;
    they are never written to a public response or manifest.  Content digests
    are handled separately from the normal scan.
    """

    paths: set[str] = set()
    task_dir = Path(task_dir).expanduser().resolve(strict=False)
    try:
        if (not task_dir.is_dir() or task_dir.is_symlink()):
            return paths
        candidates = [
            item for item in task_dir.iterdir()
            if item.is_file() and not item.is_symlink()
            and item.suffix.lower() == ".json"
            and not item.name.startswith(".")
        ]
    except (OSError, RuntimeError):
        return paths

    # Match the normal scanner's bounded input policy and duplicate-envelope
    # projection.  The output fallback must not become a second unbounded
    # task-directory crawler or retain references from a stale legacy JSON.
    candidates.sort(key=lambda item: (item.name.casefold(), item.name))
    selected: dict[str, tuple[Path, dict[str, Any], tuple[int, int, int, str]]] = {}
    for task_file in candidates[:1000]:
        task, _error, _digest = load_task(task_file)
        if task is None:
            continue
        task_id = task_id_for(task, task_file)
        candidate_key = _task_candidate_key(task_file, task)
        prior = selected.get(task_id)
        if prior is None or candidate_key > prior[2]:
            selected[task_id] = (task_file, task, candidate_key)

    for task_file, task, _candidate_key in selected.values():
        status = task_status(task)
        if status not in TERMINAL_STATUSES:
            try:
                if not generation_completion_confirmed(task):
                    continue
            except Exception:
                continue
        try:
            values = iter_video_references(task)
        except Exception:
            continue
        for value in values:
            if not isinstance(value, str):
                continue
            text = value.strip()
            if not text or re.match(r"^(?:https?|s3|data):", text, re.I) or text.startswith("//"):
                continue
            # Prefer the adapter's approved-root resolution when available.
            # This covers Capafy virtual paths and configured output roots.
            try:
                resolved = approved_local_file(
                    text, task, task_file, VIDEO_EXTS,
                    allow_video_signature=True,
                )
            except Exception:
                resolved = None
            if resolved is not None:
                try:
                    paths.add(str(resolved.resolve(strict=False)).casefold())
                except (OSError, RuntimeError):
                    paths.add(str(resolved).casefold())

            # Also compare a directly named existing path.  A task can carry
            # an explicit output path that is not yet listed in the adapter's
            # configured roots; metadata-only comparison still prevents a
            # second import when the same file is present in output_dirs.
            try:
                raw = Path(text.split("?", 1)[0].split("#", 1)[0]).expanduser()
                direct = raw if raw.is_absolute() else task_file.parent / raw
                if direct.is_file() and not direct.is_symlink():
                    paths.add(str(direct.resolve(strict=False)).casefold())
            except (OSError, RuntimeError, ValueError):
                pass

    return paths


def _received_clip_digests(result: dict[str, Any]) -> set[str]:
    """Extract public clip digests from one normal task-scan result."""

    digests: set[str] = set()
    rows = result.get("received") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return digests
    for row in rows:
        if not isinstance(row, dict):
            continue
        outputs = row.get("outputs")
        if not isinstance(outputs, list):
            continue
        for item in outputs:
            if not isinstance(item, dict) or item.get("kind") != "clip":
                continue
            value = item.get("sha256")
            if not isinstance(value, str):
                continue
            digest = value.strip().lower()
            if digest.startswith("sha256:"):
                digest = digest[7:]
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                digests.add(digest)
    return digests


def _write_active_project(root: Path, slug: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".active.", suffix=".txt", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(slug + "\n")
        os.replace(temporary, root / "active_project")
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def auto_project_for_task(
    root: Path,
    task: dict[str, Any],
    task_id: str,
    task_file: Path,
    status: str,
    mapping_error: str,
) -> str | None:
    """Create a project for a completed local or remote result when no target exists.

    This is intentionally limited to the video-first case: a terminal success
    with a usable local or cacheable remote video and no explicit project mapping. Invalid explicit
    mappings continue to use the pending queue instead of being overridden.
    """
    if mapping_error not in {"当前项目未选择。", "当前项目不存在。"}:
        return None
    has_video = task_has_video_output(task, task_file)
    normal_success = status in {"complete", "completed", "success", "succeeded"}
    # Older workers can leave the outer task marked ``failed`` after rendering
    # completed even though a usable local/cacheable video survived.  The
    # handoff promotes that recoverable result to ``completed``; allow the
    # video-first flow to reach the handoff by creating its deterministic
    # project instead of leaving it stuck behind a missing active project.
    recoverable_completed = generation_completion_confirmed(task) and has_video
    if not normal_success and not recoverable_completed:
        return None
    if not has_video:
        return None
    slug = _auto_project_slug(task_id, task)
    if project_exists(root, slug):
        _write_active_project(root, slug)
        return slug
    try:
        project = P(str(root), slug)
    except (OSError, SystemExit):
        return None
    try:
        root.mkdir(parents=True, exist_ok=True)
        os.makedirs(project.dir, exist_ok=False)
    except FileExistsError:
        return slug if project_exists(root, slug) else None
    except OSError:
        return None
    try:
        ensure_dirs(project.dir)
        document = empty_project(slug, _auto_project_title(task, task_id), "generate")
        jwrite(project.pj, document)
        project.state(phase="draft")
        project.save(document, "project.created", {"title": document["title"], "origin": "video-generator"})
        _write_active_project(root, slug)
        return slug
    except (Exception, SystemExit):
        # Keep the partially created directory recoverable for a later scan;
        # never remove user-visible files as part of an automatic handoff.
        return slug if project_exists(root, slug) else None


def ensure_runtime(root: Path, port: int, bind: str, *, timeout: float = 15.0) -> dict[str, Any]:
    """Invoke the existing launcher without exposing its output to projects."""
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "assets" / "webapp" / "ensure.py",
        here.parent / "ensure.py",
        root / "webapp" / "ensure.py",
    ]
    ensure_script = next((candidate for candidate in candidates if candidate.is_file()), None)
    if ensure_script is None:
        return {"ok": False, "error": "管理运行时入口不可用。"}
    # The scanner is itself called by ensure.py during the default start/open
    # path.  ``--no-sync`` keeps this nested probe one-way and prevents a
    # scanner -> ensure -> scanner recursion loop.
    args = [str(ensure_script), "--root", str(root), "--port", str(port),
            "--bind", bind, "--no-sync"]
    commands: list[list[str]] = []

    def add_command(command: list[str]) -> None:
        if not command:
            return
        try:
            key = os.path.normcase(os.path.abspath(command[0]))
        except (OSError, TypeError, ValueError):
            key = str(command[0]).casefold()
        for existing in commands:
            try:
                existing_key = os.path.normcase(os.path.abspath(existing[0]))
            except (OSError, TypeError, ValueError):
                existing_key = str(existing[0]).casefold()
            if existing_key == key:
                return
        commands.append(command)

    # Prefer a normal Windows launcher over an embedded interpreter (notably
    # LibreOffice's Python), which can report an invalid-handle warning after
    # starting the detached manager.  Keep PATH/current interpreters as
    # fallbacks so installations without ``py.exe`` remain supported.
    if os.name == "nt":
        launcher = shutil.which("py")
        if launcher:
            add_command([launcher, "-3", *args])
        for name in ("python", "python3"):
            normal_python = shutil.which(name)
            if normal_python:
                add_command([normal_python, *args])
    add_command([sys.executable, *args])
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                env=manager_child_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode != 0:
            continue
        try:
            value = json.loads((completed.stdout or "").strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            value = {}
        if isinstance(value, dict) and value.get("ok") is True:
            response: dict[str, Any] = {
                "ok": True,
                "started_now": bool(value.get("started_now")),
            }
            # Only the current manager-owned HTTP mode is part of the public
            # contract.  A legacy launcher may return an old Preview mode;
            # do not project that value into a new handoff summary.
            if value.get("mode") == "managed-http":
                response["mode"] = "managed-http"
            for key in ("port", "health", "entry"):
                item = value.get(key)
                if isinstance(item, (str, int, float, bool)):
                    response[key] = item
            window = value.get("window")
            if isinstance(window, dict):
                response["window"] = {
                    key: window[key] for key in ("open", "action", "port", "route")
                    if key in window and isinstance(window[key], (str, int, float, bool))
                }
            return response
    return {"ok": False, "error": "管理运行时未能启动。"}


def invoke_handoff(root: Path, slug: str, path: Path, clip: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Call the existing handoff while suppressing incidental stdout/errors."""
    output = io.StringIO()
    try:
        with sanitized_process_environment(), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = handoff(root, slug, path, clip)
    except SystemExit:
        return None, "交接暂未完成。"
    except Exception:
        return None, "交接暂未完成。"
    if not isinstance(result, dict) or result.get("ok") is not True:
        return None, "交接暂未完成。"
    return result, None


def _task_project_and_clip(
    root: Path,
    task: dict[str, Any],
    task_id: str,
    file_stem: str,
    mapping: dict[str, Any],
    cli_project: str | None,
    cli_clip: str | None,
    use_active_project: bool = True,
) -> tuple[str | None, str | None, str | None]:
    """Resolve an explicit mapping first, then the active project."""
    if cli_project is not None:
        project = safe_slug(cli_project)
        if project is None:
            return None, None, "指定的项目无效。"
        clip = safe_clip(cli_clip) if cli_clip is not None else None
        if cli_clip is not None and clip is None:
            return None, None, "指定的片段无效。"
        return project, clip, None

    mapped_project, mapped_clip, mapped = mapping_values(mapping, task_id, file_stem, task)
    if mapped:
        if mapped_project is None:
            return None, None, "任务映射无效。"
        # A prepared map is authoritative for its reservation.  Do not let a
        # provider's conflicting/unsafe clip alias strand the result when the
        # map already contains a validated clip id.  If the map intentionally
        # omits a clip, retain the task's explicit clip after validation.
        if mapped_clip is not None:
            return mapped_project, mapped_clip, None
        task_clip, task_clip_specified = explicit_clip(task)
        if task_clip_specified and task_clip is None:
            return None, None, "任务片段映射无效。"
        return mapped_project, task_clip, None

    task_project, task_project_specified = explicit_project(task)
    if task_project_specified:
        if task_project is None:
            return None, None, "任务项目映射无效。"
        task_clip, task_clip_specified = explicit_clip(task)
        if task_clip_specified and task_clip is None:
            return None, None, "任务片段映射无效。"
        return task_project, task_clip, None

    if not use_active_project:
        return None, None, "当前项目未选择。"

    active, active_error = read_active_project(root)
    if not active:
        return None, None, active_error or "当前项目未选择。"
    task_clip, task_clip_specified = explicit_clip(task)
    if task_clip_specified and task_clip is None:
        return None, None, "任务片段映射无效。"
    return active, task_clip, None


def _scan_unlocked(
    root: Path,
    task_dir: Path,
    *,
    project: str | None = None,
    clip: str | None = None,
    mapping_file: Path | None = None,
    start_runtime: bool = False,
    port: int = DEFAULT_PORT,
    bind: str = DEFAULT_BIND,
    max_tasks: int | None = None,
    use_active_project: bool = True,
) -> dict[str, Any]:
    """Scan task files and receive terminal results into projects."""
    root = root.absolute()
    task_dir = task_dir.absolute()
    result: dict[str, Any] = {
        "ok": True,
        "scanned": 0,
        "eligible": 0,
        "received": [],
        "pending": [],
        "skipped": [],
    }
    # Queue maintenance is best-effort and must not block delivery.  Run it
    # before runtime probing so a missing/slow manager cannot keep stale rows.
    try:
        _prune_stale_pending(root, task_dir)
    except Exception:
        pass
    if start_runtime:
        try:
            runtime = ensure_runtime(root, port, bind)
        except Exception:
            # A manager/Preview probe must never prevent a completed result
            # from being registered in the filesystem.
            runtime = {"ok": False, "error": "管理运行时未能启动。"}
        if not isinstance(runtime, dict):
            runtime = {"ok": False, "error": "管理运行时未能启动。"}
        # Runtime startup is a convenience for the management window, not a
        # prerequisite for receiving a generator result.  A port conflict,
        # Preview outage, or missing launcher must therefore remain a warning
        # while the filesystem handoff continues below.  Keep ``ok`` true so
        # callers do not mistake a manager-only problem for a generation
        # failure; expose the bounded runtime payload for diagnostics.
        result["runtime"] = runtime
        if not runtime.get("ok"):
            result["manager_warning"] = str(
                runtime.get("error") or "管理运行时未能启动。"
            )[:240]

    if not task_dir.exists() or not task_dir.is_dir() or task_dir.is_symlink():
        sync_index(str(root))
        return result

    default_map = root / ".handoff_map.json"
    if mapping_file is None and default_map.is_file():
        mapping_file = default_map
    mapping = load_mapping(mapping_file)
    pending = read_pending(root)

    try:
        paths = sorted(
            (entry for entry in task_dir.iterdir()
             if entry.is_file() and not entry.is_symlink()
             and entry.suffix.lower() == ".json"
             and not entry.name.startswith(".")),
            key=lambda item: (item.name.casefold(), item.name),
        )
    except OSError:
        paths = []
    if max_tasks is not None:
        try:
            limit = max(1, min(int(max_tasks), 1000))
            paths = paths[:limit]
        except (TypeError, ValueError):
            paths = paths[:100]

    # Load each envelope once and collapse legacy/current files that carry the
    # same stable task ID.  Without this projection an old generator envelope
    # (video only) and a newer generic envelope (video + public script) would
    # alternate on every watcher pass, rewriting the task summary and project
    # revision forever.  The newest terminal envelope wins, with public-field
    # richness and filename as deterministic tie-breakers.
    result["scanned"] = len(paths)
    candidates: dict[str, tuple[Path, dict[str, Any], tuple[int, int, int, str]]] = {}
    for path in paths:
        task, load_error, _private_digest = load_task(path)
        if task is None:
            # Invalid/incomplete files are ignored until the worker has a
            # complete atomic write; do not create a noisy public queue entry.
            result["skipped"].append({
                "task_id": safe_task_id(path.stem, path.stem),
                "reason": load_error or "任务文件不可用。",
            })
            continue
        task_id = task_id_for(task, path)
        key = _task_candidate_key(path, task)
        previous = candidates.get(task_id)
        if previous is None or key > previous[2]:
            candidates[task_id] = (path, task, key)

    for task_id, (path, task, _candidate_key) in sorted(
        candidates.items(), key=lambda item: (item[0].casefold(), item[0])
    ):
        status = task_status(task)
        if status not in TERMINAL_STATUSES:
            result["skipped"].append({"task_id": task_id, "reason": "任务尚未完成。"})
            continue
        result["eligible"] += 1
        project_slug, clip_id, mapping_error = _task_project_and_clip(
            root, task, task_id, path.stem, mapping, project, clip,
            use_active_project=use_active_project,
        )
        if mapping_error:
            # A completed local generator result is allowed to become a new project
            # on first intake.  This is the video-first workflow: the user can
            # generate first, then open the manager and see a project without
            # having to pre-create one.  Explicit but invalid mappings still
            # remain pending and are never overridden.
            auto_project = auto_project_for_task(root, task, task_id, path, status, mapping_error)
            if auto_project:
                project_slug = auto_project
                mapping_error = None
            else:
                pending_add(pending, task_id, status, mapping_error)
                result["pending"].append({
                    "task_id": task_id, "status": status, "reason": mapping_error,
                    "category": pending_category(mapping_error),
                })
                continue
        if not project_exists(root, project_slug):
            reason = "目标项目不存在。"
            pending_add(pending, task_id, status, reason)
            result["pending"].append({
                "task_id": task_id, "status": status, "project": project_slug,
                "reason": reason, "category": pending_category(reason),
            })
            continue

        handoff_result, handoff_error = invoke_handoff(root, project_slug or "", path, clip_id)
        if handoff_error or handoff_result is None:
            reason = handoff_error or "交接暂未完成。"
            pending_add(pending, task_id, status, reason)
            result["pending"].append({
                "task_id": task_id, "status": status, "project": project_slug,
                "reason": reason, "category": pending_category(reason),
            })
            continue
        pending.pop(task_id, None)
        received = {
            "task_id": task_id,
            "project": project_slug,
            "clip_id": handoff_result.get("clip_id", clip_id),
            "status": handoff_result.get("status", status),
            "idempotent": bool(handoff_result.get("idempotent")),
            "outputs": handoff_result.get("outputs", []),
        }
        # Surface only the public project-relative document references in the
        # scan result.  This lets callers confirm that the generator's
        # original input script was attached without exposing the source task
        # JSON or any private prompt/provider fields.
        for key in ("report", "script", "message"):
            value = handoff_result.get(key)
            if isinstance(value, str):
                received[key] = value[:500]
        audio = handoff_result.get("audio")
        if isinstance(audio, dict):
            compact_audio: dict[str, str] = {}
            for key in ("status", "file", "message"):
                value = audio.get(key)
                if isinstance(value, str):
                    compact_audio[key] = value[:500]
            if compact_audio:
                received["audio"] = compact_audio
        result["received"].append(received)

    write_pending(root, pending)
    sync_index(str(root))
    # Keep the queue output useful even when a previous pending item was
    # resolved in this pass; no task file paths are ever emitted.
    result["pending_count"] = len(pending)
    result["unmatched_count"] = sum(
        1 for item in pending.values()
        if isinstance(item, dict)
        and pending_category(item.get("reason"), item.get("category")) == "unmatched_project"
    )
    result["delivery_failed_count"] = max(
        0, len(pending) - int(result["unmatched_count"])
    )
    return result


def scan(
    root: Path,
    task_dir: Path,
    *,
    project: str | None = None,
    clip: str | None = None,
    mapping_file: Path | None = None,
    start_runtime: bool = False,
    port: int = DEFAULT_PORT,
    bind: str = DEFAULT_BIND,
    max_tasks: int | None = None,
    use_active_project: bool = True,
    include_outputs: bool = False,
    output_dirs: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Serialize and run one generator-handoff scan.

    The public function keeps the historical signature used by ``receive``
    and tests.  A second manager process that arrives while another process is
    receiving simply reports a bounded, non-error skip; its next watcher tick
    will retry after the first process releases the lock.
    """

    root = Path(root).absolute()
    with scan_lock(root) as claimed:
        if not claimed:
            return {
                "ok": True,
                "scanned": 0,
                "eligible": 0,
                "received": [],
                "pending": [],
                "skipped": [],
                "pending_count": len(read_pending(root)),
                "unmatched_count": 0,
                "delivery_failed_count": 0,
                "skipped_reason": "scan_in_progress",
            }
        base_result = _scan_unlocked(
            root,
            Path(task_dir),
            project=project,
            clip=clip,
            mapping_file=mapping_file,
            start_runtime=start_runtime,
            port=port,
            bind=bind,
            max_tasks=max_tasks,
            use_active_project=use_active_project,
        )
    if not claimed:
        return base_result
    if not include_outputs:
        return base_result
    # A task inbox and a generator's output bucket may be separate folders.
    # Carry terminal task references into the fallback scan so a bare copy of
    # the same result is not imported a second time.
    referenced_paths = _task_video_reference_paths(Path(task_dir))
    task_clip_digests = _received_clip_digests(base_result)
    output_result = scan_output_dirs(
        root,
        output_dirs=output_dirs,
        project=project,
        clip=clip,
        exclude_paths=referenced_paths,
        exclude_digests=task_clip_digests,
    )
    # Merge only bounded public aggregates; the nested receiver may have
    # refreshed the canonical pending queue and index in the meantime.
    for key in ("received", "pending", "skipped"):
        values = output_result.get(key)
        if isinstance(values, list):
            current = base_result.setdefault(key, [])
            if isinstance(current, list):
                current.extend(values[:25])
                del current[25:]
    for key in ("scanned", "eligible", "imported", "idempotent"):
        try:
            base_result[key] = int(base_result.get(key, 0) or 0) + int(output_result.get(key, 0) or 0)
        except (TypeError, ValueError):
            pass
    try:
        base_result["failed"] = int(base_result.get("failed", 0) or 0) + int(output_result.get("failed", 0) or 0)
    except (TypeError, ValueError):
        pass
    for key in ("pending_count", "unmatched_count", "delivery_failed_count"):
        if key in output_result:
            base_result[key] = output_result[key]
    if "output_dirs" in output_result:
        base_result["output_dirs"] = output_result["output_dirs"]
    return base_result


def _output_state_path(root: Path) -> Path:
    return Path(root).absolute() / OUTPUT_WATCH_STATE_NAME


def _load_output_state(root: Path) -> dict[str, tuple[int, int]]:
    """Load private duplicate-suppression state without retaining paths."""

    path = _output_state_path(root)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 512 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    files = value.get("files") if isinstance(value, dict) else None
    if not isinstance(files, dict):
        return {}
    result: dict[str, tuple[int, int]] = {}
    for key, item in list(files.items())[:MAX_OUTPUT_STATE]:
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            continue
        if not isinstance(item, dict):
            continue
        try:
            mtime = int(item.get("mtime_ns"))
            size = int(item.get("size"))
        except (TypeError, ValueError):
            continue
        if mtime >= 0 and size >= 0:
            result[key] = (mtime, size)
    return result


def _save_output_state(root: Path, state: dict[str, tuple[int, int]]) -> None:
    """Atomically write bounded private watcher state (no absolute paths)."""

    path = _output_state_path(root)
    clean = {
        key: {"mtime_ns": int(value[0]), "size": int(value[1])}
        for key, value in list(state.items())[-MAX_OUTPUT_STATE:]
        if isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)
    }
    payload = {"schema": 1, "files": clean}
    root.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".output-watch.", suffix=".json", dir=str(root))
        temporary = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(str(temporary), str(path))
        temporary = None
    except OSError:
        # A read-only manager root must not make the web runtime fail.  The
        # next scan simply retries and the handoff's own task digest remains
        # the final idempotency guard.
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _output_file_key(path: Path) -> str:
    try:
        normalized = str(path.resolve(strict=False)).casefold()
    except (OSError, RuntimeError):
        normalized = str(path).casefold()
    # Only the digest is persisted; absolute source paths never cross the
    # customer-visible project boundary.
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()


def _compact_output_result(value: object) -> dict[str, Any] | None:
    """Keep only the same public fields exposed by normal task scans."""

    if not isinstance(value, dict):
        return None
    row: dict[str, Any] = {}
    for key in ("task_id", "project", "clip_id", "status", "idempotent", "report", "script", "message"):
        item = value.get(key)
        if isinstance(item, str):
            row[key] = item[:500]
        elif isinstance(item, bool):
            row[key] = item
    outputs = value.get("outputs")
    if isinstance(outputs, list):
        row["outputs"] = [
            {
                key: entry[key]
                for key in ("file", "kind", "sha256")
                if isinstance(entry, dict) and isinstance(entry.get(key), str)
            }
            for entry in outputs[:20]
            if isinstance(entry, dict)
        ]
    audio = value.get("audio")
    if isinstance(audio, dict):
        compact_audio: dict[str, str] = {}
        for key in ("status", "file", "message"):
            item = audio.get(key)
            if isinstance(item, str):
                compact_audio[key] = item[:500]
        if compact_audio:
            row["audio"] = compact_audio
    return row


def _output_result_terminal(value: object) -> bool:
    """Return true only for a delivered or idempotent public result."""

    if not isinstance(value, dict) or value.get("ok") is not True:
        return False
    rows = value.get("received")
    if not isinstance(rows, list) or not rows:
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("idempotent") is True:
            return True
        outputs = row.get("outputs")
        if isinstance(outputs, list) and any(
            isinstance(item, dict) and isinstance(item.get("file"), str)
            for item in outputs
        ):
            return True
    return False


def scan_output_dirs(
    root: Path,
    output_dirs: Iterable[Path] | None = None,
    *,
    project: str | None = None,
    clip: str | None = None,
    max_files: int = 100,
    exclude_paths: Iterable[Path] | None = None,
    exclude_names: Iterable[str] | None = None,
    exclude_digests: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Receive stable bare videos/result envelopes from bounded output dirs.

    This is a fallback for generators that do not emit the manager's JSON
    handoff.  It runs after the normal task scan has released its lock, so the
    receiver can safely call the ordinary handoff path without recursive lock
    contention.  Every source is still validated by ``vpm_receive.receive``;
    this function only discovers direct children of explicitly configured or
    conventional output buckets.
    """

    root = Path(root).absolute()
    dirs = list(output_dirs) if output_dirs is not None else resolve_output_dirs(manager_root=root)
    result: dict[str, Any] = {
        "ok": True,
        "scanned": 0,
        "eligible": 0,
        "received": [],
        "pending": [],
        "skipped": [],
        "output_dirs": min(len(dirs), MAX_OUTPUT_DIRS),
    }
    if not dirs:
        result["skipped_reason"] = "output_dirs_unavailable"
        result["pending_count"] = len(read_pending(root))
        return result

    # Import lazily to avoid the vpm_receive -> vpm_sync import cycle during
    # module initialization.
    try:
        import vpm_receive  # type: ignore[import-not-found]
    except Exception:
        result["ok"] = False
        result["error"] = "output receiver unavailable"
        result["failed"] = 1
        return result

    state = _load_output_state(root)
    excluded_paths: set[str] = set()
    for value in exclude_paths or ():
        try:
            candidate = Path(value).expanduser().resolve(strict=False)
            excluded_paths.add(str(candidate).casefold())
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    excluded_names = {
        str(value).strip().casefold()
        for value in (exclude_names or ())
        if isinstance(value, str) and str(value).strip()
    }
    excluded_digests: set[str] = set()
    for value in exclude_digests or ():
        if not isinstance(value, str):
            continue
        digest = value.strip().lower()
        if digest.startswith("sha256:"):
            digest = digest[7:]
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            excluded_digests.add(digest)
    try:
        limit = max(1, min(int(max_files), 1000))
    except (TypeError, ValueError):
        limit = 100
    seen_this_pass = 0

    for directory in dirs[:MAX_OUTPUT_DIRS]:
        try:
            directory = Path(directory).expanduser().resolve(strict=False)
            if not _usable_output_dir(directory, root):
                continue
            if not directory.is_dir() or directory.is_symlink():
                continue
            discover = getattr(vpm_receive, "discover_output_candidates", None)
            if callable(discover):
                candidates = discover(directory, limit=MAX_OUTPUT_STATE)
            else:
                candidates = vpm_receive._watch_inputs(directory)  # old bundle fallback
        except (OSError, RuntimeError, ValueError):
            continue
        for candidate in candidates:
            if seen_this_pass >= limit:
                break
            if candidate.suffix.lower() in getattr(vpm_receive, "VIDEO_EXTS", VIDEO_EXTS):
                try:
                    candidate_key = str(candidate.resolve(strict=False)).casefold()
                except (OSError, RuntimeError):
                    candidate_key = str(candidate).casefold()
                if (candidate_key in excluded_paths
                        or candidate.name.casefold() in excluded_names):
                    # The normal task scan owns this file.  Leave it out of
                    # output-watch state so a later pass can import it if the
                    # task's delivery becomes retryable; this pass merely
                    # avoids creating a duplicate clip/project.
                    result["skipped"].append({
                        "file": candidate.name,
                        "reason": "output_referenced_by_task",
                    })
                    continue
            try:
                stat = candidate.stat()
                signature = (int(stat.st_mtime_ns), int(stat.st_size))
            except OSError:
                continue
            key = _output_file_key(candidate)
            if state.get(key) == signature:
                continue
            result["scanned"] += 1
            seen_this_pass += 1
            # Avoid consuming a file while the generator is still copying it.
            try:
                time.sleep(0.05)
                check = candidate.stat()
                if (int(check.st_mtime_ns), int(check.st_size)) != signature:
                    result["skipped"].append({"reason": "output_still_writing"})
                    continue
            except OSError:
                continue
            # Hash only after the short stability check above.  Reading a
            # multi-gigabyte artifact while its producer is still writing it
            # would waste I/O and could compare an incomplete digest.
            if excluded_digests:
                try:
                    digest = hashlib.sha256()
                    with candidate.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    candidate_digest = digest.hexdigest()
                except OSError:
                    candidate_digest = ""
                if candidate_digest in excluded_digests:
                    result["skipped"].append({
                        "file": candidate.name,
                        "reason": "output_matches_task",
                    })
                    continue
            try:
                received = vpm_receive.receive(
                    candidate,
                    root=root,
                    project=project,
                    clip=clip,
                    generator="video-generator",
                    start_runtime=False,
                    # A file-only fallback has no trustworthy project context.
                    # Explicit task/map/CLI project values still win, but a
                    # stale UI active project must not absorb an unrelated
                    # generator's new output.
                    use_active_project=bool(project),
                )
            except Exception:
                result["failed"] = int(result.get("failed", 0) or 0) + 1
                continue
            if not isinstance(received, dict):
                result["failed"] = int(result.get("failed", 0) or 0) + 1
                continue
            rows = received.get("received")
            compact_rows = []
            if isinstance(rows, list):
                for row in rows[:25]:
                    compact = _compact_output_result(row)
                    if compact is not None:
                        compact_rows.append(compact)
            result["received"].extend(compact_rows)
            pending_rows = received.get("pending")
            if isinstance(pending_rows, list):
                result["pending"].extend(
                    item for item in pending_rows[:25] if isinstance(item, dict)
                )
            if compact_rows and _output_result_terminal(received):
                result["eligible"] += 1
                result["imported"] = int(result.get("imported", 0) or 0) + sum(
                    1 for row in compact_rows if not row.get("idempotent")
                )
                result["idempotent"] = int(result.get("idempotent", 0) or 0) + sum(
                    1 for row in compact_rows if row.get("idempotent")
                )
                state[key] = signature
            else:
                # A pending/partial result remains eligible for a later pass.
                result["pending_count"] = int(result.get("pending_count", 0) or 0) + 1
        if seen_this_pass >= limit:
            break

    _save_output_state(root, state)
    # The canonical queue is the source of truth after nested receives.
    pending_items = read_pending(root)
    result["pending_count"] = len(pending_items)
    result["unmatched_count"] = sum(
        1 for item in pending_items.values()
        if isinstance(item, dict)
        and pending_category(item.get("reason"), item.get("category")) == "unmatched_project"
    )
    result["delivery_failed_count"] = max(
        0, len(pending_items) - int(result["unmatched_count"])
    )
    result["received"] = result["received"][:25]
    result["pending"] = result["pending"][:25]
    sync_index(str(root))
    return result


def parse_port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("端口无效。") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口无效。")
    return port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None, help="manager data root")
    parser.add_argument("--task-dir", default=None, help="video-generator handoff inbox")
    parser.add_argument("--project", default=None, help="explicit project slug for all terminal tasks")
    parser.add_argument("--clip", default=None, help="optional clip id for all terminal tasks")
    parser.add_argument("--map", dest="mapping_file", default=None, help="optional task-to-project JSON map")
    parser.add_argument("--port", type=parse_port, default=DEFAULT_PORT)
    parser.add_argument("--bind", default=DEFAULT_BIND)
    parser.add_argument("--max-tasks", type=int, default=None,
                        help="maximum task files to inspect")
    parser.add_argument(
        "--output-dir", action="append", default=None,
        help="bounded generator output directory (repeatable; env defaults are also used)",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="scan only; do not start/probe the manager runtime",
    )
    args = parser.parse_args(argv)

    # The forced target project applies to EVERY scanned task, so a queued task that

    # carries no project of its own used to be re-ingested into whatever project

    # was named here -- that is how one project's footage appeared in another.

    # Only tolerate it when the caller proves the batch holds a single task.

    if getattr(args, "project", None):

        if getattr(args, "max_tasks", None) != 1:

            print(json.dumps({"ok": False, "error": "refusing --project without --max-tasks 1: "

                                 "the forced target would apply to every queued task"},

                                ensure_ascii=False))

            return 2

    root = resolve_root(args.root)
    task_dir = resolve_task_dir(args.task_dir)
    if args.task_dir is None and task_dir == DEFAULT_TASK_DIR:
        inbox = root / "inbox"
        if inbox.is_dir() and not inbox.is_symlink():
            task_dir = inbox
    mapping = Path(args.mapping_file).expanduser().absolute() if args.mapping_file else None
    result = scan(
        root,
        task_dir,
        project=args.project,
        clip=args.clip,
        mapping_file=mapping,
        start_runtime=not args.no_start,
        port=args.port,
        bind=str(args.bind or DEFAULT_BIND).strip() or DEFAULT_BIND,
        max_tasks=args.max_tasks,
        include_outputs=True,
        output_dirs=(
            [Path(item).expanduser().absolute() for item in args.output_dir]
            if args.output_dir else None
        ),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
