#!/usr/bin/env python3
"""Start or probe the single video-asset-manager runtime.

The manager is one long-lived, filesystem-backed service for all projects. It
is safe to call this script repeatedly: a healthy service is reused and a new
process is started only when the health endpoint is unavailable. The manager
owns its fixed application port directly and does not register a second
application.

The runtime manifest is kept in ``<root>/webapp/runtime.json`` for the local
launcher. ``server.py`` deliberately denies that path (and ``server.log``)
from its static file handler, so process details are never part of the web
application's public surface.
"""
from __future__ import annotations

import argparse
import contextlib
import filecmp
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Iterator


HERE = Path(__file__).resolve().parent
SOURCE_SCRIPTS = HERE.parent.parent / "scripts"
if SOURCE_SCRIPTS.is_dir() and str(SOURCE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SOURCE_SCRIPTS))
# The installed runtime is copied to ``<manager-root>/webapp`` and may later
# be invoked directly from there.  Keep the scanner and its two bookkeeping
# dependencies beside the private launcher so the one-step sync still works
# after the skill source directory is not on ``sys.path``.
BUNDLE = ("server.py", "index.html", "studio-skin.css", "inter.ttf", "roboto.ttf", "playfairdisplay.ttf", "bebasneue.ttf", "dancingscript.ttf", "workspace.js", "studio-workspace.js", "studio-panels.js", "studio-filmstrip.js", "ensure.py", "vpm_privacy.py", "vpm_context.py", "vpm_sync.py", "handoff_core.py", "handoff.py", "receive.py", "vpm_receive.py", "handoff_impl.py", "vpm_record.py", "vpm_prepare.py")
# Files from retired integrations must not survive in an installed runtime:
# an old module can select the wrong launcher.
BUNDLE += ("inter-OFL.txt",)
BUNDLE += (
    "licenses/README.md", "licenses/inter-OFL.txt", "licenses/roboto-OFL.txt",
    "licenses/playfairdisplay-OFL.txt", "licenses/bebasneue-OFL.txt",
    "licenses/dancingscript-OFL.txt", "licenses/tooscut-NOTICE.md",
    "licenses/tooscut-ELASTIC-LICENSE-2.0.txt",
    "licenses/editor-sources.md", "licenses/openreel-MIT.txt", "licenses/opencut-MIT.txt",
)
RETIRED_BUNDLE_FILES = ("preview_bridge.py",)
DEFAULT_PORT = 4200
# The manager owns its application port directly.  Binding all interfaces is
# required when the manager is opened through a forwarded port; callers may
# opt into a narrower bind with VAM_BIND.
DEFAULT_BIND = "0.0.0.0"
LOCK_TIMEOUT = 30.0
LOCK_STALE_AFTER = 180.0
DEFAULT_TASK_DIR = Path(tempfile.gettempdir()) / "video-generator-handoffs"
TERMINAL_TASK_STATUSES = {
    "complete", "completed", "success", "succeeded", "done",
    "failed", "error", "cancelled", "canceled", "timed_out", "timeout",
    "partial",
}
PROJECT_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
MAX_TASK_SCAN_BYTES = 8 * 1024 * 1024
MAX_SCAN_TASKS = 100
# The manager only serves local project files.  Do not inherit credentials
# that belong to any video/image provider or object-storage uploader when the
# long-lived child process is launched.  The parent generation process keeps
# its own environment unchanged.
PRIVATE_CHILD_ENV_NAMES = (
    "OPENAI_API_KEY", "ARK_API_KEY",
    "TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_SESSION_TOKEN",
    "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "VIDEO_GENERATOR_API_KEY",
)

_UNMATCHED_REASON_MARKERS = (
    "当前项目未选择", "当前项目不存在", "当前项目不可用", "目标项目不存在",
    "指定的项目", "任务映射", "项目映射", "项目 slug", "项目无效",
)


def _pending_row_counts(rows: object) -> tuple[int, int]:
    """Return ``(unmatched, delivery_failed)`` for queue rows."""

    if not isinstance(rows, list):
        return 0, 0
    unmatched = delivery_failed = 0
    for item in rows[:10000]:
        if not isinstance(item, dict):
            continue
        if item.get("ok") is True:
            continue
        category = str(item.get("category") or "").strip().lower()
        reason = str(item.get("reason") or "").strip().lower()
        is_unmatched = category in {"unmatched", "unmatched_project", "project_mapping"}
        if not is_unmatched:
            is_unmatched = any(marker.lower() in reason for marker in _UNMATCHED_REASON_MARKERS)
        if is_unmatched:
            unmatched += 1
        else:
            delivery_failed += 1
    return min(unmatched, 10000), min(delivery_failed, 10000)


try:
    from vpm_privacy import (  # type: ignore[import-not-found]
        manager_child_environment,
        sanitized_process_environment,
    )
except Exception:  # pragma: no cover - old copied bundle fallback
    @contextlib.contextmanager
    def sanitized_process_environment() -> Iterator[None]:
        original = dict(os.environ)
        try:
            safe = {
                name: value for name, value in original.items()
                if not re.search(
                    r"(?:API[_-]?KEY|ACCESS[_-]?KEY|SECRET|TOKEN|PASSWORD|AUTH|CREDENTIAL|PRIVATE[_-]?KEY|SIGNATURE)",
                    str(name), re.I,
                )
            }
            os.environ.clear()
            os.environ.update(safe)
            yield
        finally:
            os.environ.clear()
            os.environ.update(original)

    def manager_child_environment() -> dict[str, str]:
        """Conservative fallback for an old bundle without vpm_privacy.py."""

        with sanitized_process_environment():
            return dict(os.environ)


def resolve_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the manager root using the skill's shared precedence order."""

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
        return Path(
            os.path.abspath(
                os.path.join(os.path.expanduser(workspace.strip()), ".capafy", "video-asset-manager")
            )
        )

    # Keep this fallback in lock-step with server.py and vpm_record.py. On
    # Windows Path.home() resolves to %USERPROFILE%; on POSIX it resolves to
    # $HOME.
    return Path.home() / "workspace" / ".capafy" / "video-asset-manager"


def default_root() -> str:
    """Compatibility helper matching server.py and vpm_record.py."""

    return str(resolve_root())


def _valid_port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def env_port() -> int:
    """Read compatible port variables, falling back safely to 4200."""

    for variable in ("VAM_PORT", "VPM_PORT"):
        value = os.environ.get(variable)
        if not value:
            continue
        try:
            return _valid_port(value)
        except argparse.ArgumentTypeError:
            # A malformed optional environment variable should not prevent the
            # manager from starting with its documented default.
            continue
    return DEFAULT_PORT


def env_bind() -> str:
    value = os.environ.get("VAM_BIND")
    if value and value.strip():
        return value.strip()
    return DEFAULT_BIND


def _probe_host(bind: str | None) -> str:
    """Choose a loopback address when the server binds to all interfaces."""

    value = (bind or DEFAULT_BIND).strip()
    if value in {"", "0.0.0.0", "::", "[::]", "*"}:
        return "127.0.0.1"
    return value.strip("[]")


def _url_host(host: str) -> str:
    # urllib requires IPv6 literals to be enclosed in brackets in a URL.
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def probe(port: int, timeout: float = 2.0, bind: str | None = None) -> bool:
    """Return True only when the manager health endpoint reports ``ok``."""

    host = _probe_host(bind)
    try:
        with urllib.request.urlopen(
            f"http://{_url_host(host)}:{int(port)}/api/health", timeout=timeout
        ) as response:
            if getattr(response, "status", 200) != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                return False
            # Older bundles returned only {ok:true}; accept those for a smooth
            # upgrade, but avoid treating another service's health endpoint as
            # our runtime when it identifies itself explicitly.
            service = payload.get("service")
            return not service or service == "video-asset-manager"
    except Exception:
        return False


def probe_details(port: int, timeout: float = 2.0, bind: str | None = None) -> dict:
    """Classify the listener without treating an unrelated HTTP service as ours."""
    host = _probe_host(bind)
    try:
        with urllib.request.urlopen(f"http://{_url_host(host)}:{int(port)}/api/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                return {"process": "unknown", "port": "occupied", "ownership": "unknown", "http": "invalid"}
            ours = payload.get("service") == "video-asset-manager" or payload.get("runtime_id") == "video-asset-manager"
            return {"process": "running", "port": "occupied", "ownership": "self" if ours else "other",
                    "http": "healthy" if payload.get("ok") is True else "unhealthy", "payload": payload}
    except urllib.error.HTTPError as exc:
        return {"process": "running", "port": "occupied", "ownership": "other", "http": f"http_{exc.code}"}
    except (OSError, ValueError, json.JSONDecodeError):
        return {"process": "unknown", "port": "unknown", "ownership": "unknown", "http": "unreachable"}


def read_runtime(rt_path: str | os.PathLike[str]) -> dict:
    try:
        with open(rt_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _path_within(base: Path, candidate: Path) -> bool:
    """Resolve both paths and require *candidate* to remain below *base*."""

    try:
        base_value = os.path.normcase(str(base.resolve(strict=False)))
        candidate_value = os.path.normcase(str(candidate.resolve(strict=False)))
        return os.path.commonpath((base_value, candidate_value)) == base_value
    except (OSError, RuntimeError, ValueError):
        return False


def _valid_project_slug(root: Path, value: object) -> str | None:
    """Validate an explicit/active project without following a boundary symlink."""

    slug = str(value or "").strip()
    if not PROJECT_SLUG_RE.fullmatch(slug) or slug in {"webapp", "trash", "api", "staging"}:
        return None
    project_dir = root / slug
    manifest = project_dir / "project.json"
    try:
        if project_dir.is_symlink() or not project_dir.is_dir() or not _path_within(root, project_dir):
            return None
        if manifest.is_symlink() or not manifest.is_file() or not _path_within(project_dir, manifest):
            return None
    except OSError:
        return None
    return slug


def _active_project(root: Path, explicit: str | None = None) -> str | None:
    if explicit is not None:
        return _valid_project_slug(root, explicit)
    marker = root / "active_project"
    try:
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 256:
            return None
        text = marker.read_text(encoding="utf-8")
        value = text.strip().splitlines()[0] if text.strip() else ""
    except (OSError, UnicodeError, IndexError):
        return None
    return _valid_project_slug(root, value)


def _task_directory(requested: str | os.PathLike[str] | None = None) -> Path | None:
    value = requested
    if not value:
        for variable in ("VIDEO_GENERATOR_HANDOFF_DIR", "VIDEO_GENERATOR_TASK_DIR",
                         "VAM_HANDOFF_DIR"):
            value = os.environ.get(variable)
            if value:
                break
    candidate = Path(value).expanduser() if value else DEFAULT_TASK_DIR
    try:
        resolved = candidate.resolve(strict=False)
        if candidate.is_symlink() or not resolved.is_dir():
            return None
        return resolved
    except (OSError, RuntimeError):
        return None


def _read_task_snapshot(path: Path) -> dict | None:
    """Read a stable, bounded task JSON snapshot for status filtering."""

    try:
        if path.is_symlink() or not path.is_file():
            return None
        first = path.stat()
        if first.st_size <= 0 or first.st_size > MAX_TASK_SCAN_BYTES:
            return None
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        second = path.stat()
        if first.st_size != second.st_size or first.st_mtime_ns != second.st_mtime_ns:
            # The generator may still be atomically updating this task. Leave it for a
            # later scan instead of handing off a partial document.
            return None
        return payload if isinstance(payload, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None


def _task_status(task: dict) -> str:
    return str(task.get("status") or task.get("task_status") or "").strip().lower()


def _safe_handoff_result(result: object) -> dict:
    """Keep auto-sync output to small, user-visible fields only."""

    if not isinstance(result, dict):
        return {"ok": False, "error": "handoff returned an invalid result"}
    safe: dict = {"ok": bool(result.get("ok"))}
    for key in ("idempotent", "task_id", "clip_id", "status", "message", "report", "script"):
        value = result.get(key)
        if isinstance(value, (str, bool)):
            safe[key] = value[:500] if isinstance(value, str) else value
    outputs = result.get("outputs")
    if isinstance(outputs, list):
        safe_outputs = []
        for item in outputs[:20]:
            if not isinstance(item, dict):
                continue
            row = {}
            for key in ("file", "kind", "sha256"):
                value = item.get(key)
                if isinstance(value, str) and len(value) <= 500 and not re.match(r"^(?:https?|data):", value, re.I):
                    row[key] = value
            if row:
                safe_outputs.append(row)
        safe["outputs"] = safe_outputs
    if not safe.get("ok"):
        safe["error"] = "handoff failed"
    return safe


def _call_handoff(root: Path, project: str, task_file: Path, clip: str | None) -> dict:
    """Invoke the bundled handoff function without exposing its stdout/stderr."""

    candidates = [HERE.parent.parent / "scripts", HERE]
    scripts_dir = next(
        (candidate for candidate in candidates
         if ((candidate / "handoff_core.py").is_file() or (candidate / "handoff.py").is_file()
             or (candidate / "handoff_impl.py").is_file())
         and (candidate / "vpm_record.py").is_file()),
        None,
    )
    if scripts_dir is None:
        return {"ok": False, "error": "handoff unavailable"}
    try:
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        try:
            from handoff_core import handoff  # type: ignore[import-not-found]
        except Exception:
            try:
                from handoff import handoff  # type: ignore[import-not-found]
            except Exception:
                from handoff_impl import handoff  # type: ignore[import-not-found]
    except Exception:
        return {"ok": False, "error": "handoff unavailable"}
    try:
        # The helper is also a CLI and may print a sanitized failure before
        # raising SystemExit. Suppress that stream when called from ensure so
        # only the compact result is returned to the caller.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = handoff(root, project, task_file, clip)
        return _safe_handoff_result(result)
    except SystemExit:
        return {"ok": False, "error": "handoff rejected"}
    except Exception:
        return {"ok": False, "error": "handoff failed"}


def _sync_with_vpm_sync(root: Path, project: str | None,
                        task_dir: str | os.PathLike[str] | None,
                        clip: str | None, max_tasks: int,
                        mapping_file: str | os.PathLike[str] | None) -> dict | None:
    """Use the full skill scanner when this launcher runs from the skill.

    ``ensure.py`` is copied into the manager's private runtime directory.  A
    copied launcher may not have the sibling ``scripts`` directory, so the
    small legacy scanner below remains as a fallback for that case.  The source
    launcher (the documented entrypoint) gets explicit task mappings, pending
    handoff tracking, and conflict-safe destination selection from
    ``scripts/vpm_sync.py``.
    """
    candidates = [HERE.parent.parent / "scripts", HERE]
    scripts_dir = next((candidate for candidate in candidates
                        if (candidate / "vpm_sync.py").is_file()), None)
    if scripts_dir is None:
        return None
    try:
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        from vpm_sync import scan  # type: ignore[import-not-found]
    except Exception:
        return None
    directory = _task_directory(task_dir)
    if directory is None and not task_dir:
        inbox = root / "inbox"
        if inbox.is_dir() and not inbox.is_symlink():
            directory = inbox.resolve(strict=False)
    # A generator may deliver only a standalone video file and never create a
    # JSON handoff inbox.  Keep a non-existent sentinel for the task side so
    # the full scanner can still run its bounded ``include_outputs`` fallback
    # in the same startup call.  This preserves the explicit task-dir safety
    # boundary while making ``ensure.py open`` a true one-step intake.
    if directory is None:
        directory = root / ".vam-no-task-dir"
    map_path: Path | None = None
    if mapping_file:
        try:
            map_path = Path(mapping_file).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            map_path = None
    try:
        # The launcher may itself run inside a generator host process.  Keep
        # provider/storage credentials out of the in-process handoff scan just
        # as we do for the detached HTTP server child.
        with sanitized_process_environment():
            raw = scan(
                root,
                directory,
                project=project,
                clip=clip,
                mapping_file=map_path,
                start_runtime=False,
                max_tasks=max_tasks,
                include_outputs=True,
            )
    except Exception:
        return None
    received = raw.get("received") if isinstance(raw.get("received"), list) else []
    pending = raw.get("pending") if isinstance(raw.get("pending"), list) else []
    results: list[dict] = []
    for item in received[:25]:
        if isinstance(item, dict):
            results.append({"ok": True, **item})
    for item in pending[: max(0, 25 - len(results))]:
        if isinstance(item, dict):
            results.append({"ok": False, **item, "error": "handoff pending"})
    imported = sum(1 for item in received if isinstance(item, dict) and not item.get("idempotent"))
    idempotent = sum(1 for item in received if isinstance(item, dict) and item.get("idempotent"))
    unmatched_count, delivery_failed_count = _pending_row_counts(pending)
    try:
        unmatched_count = max(unmatched_count, int(raw.get("unmatched_count", 0) or 0))
    except (TypeError, ValueError):
        pass
    try:
        delivery_failed_count = max(delivery_failed_count, int(raw.get("delivery_failed_count", 0) or 0))
    except (TypeError, ValueError):
        pass
    response: dict = {
        "ok": bool(raw.get("ok", True)),
        "scanned": int(raw.get("eligible", 0) or 0),
        "imported": imported,
        "idempotent": idempotent,
        "pending_count": int(raw.get("pending_count", len(pending)) or 0),
        "unmatched_count": unmatched_count,
        "delivery_failed_count": delivery_failed_count,
        # ``failed`` now means scanner/runtime failure only.  Pending rows are
        # represented by the explicit category counts above.
        "failed": 0 if bool(raw.get("ok", True)) else 1,
        "results": results,
    }
    if raw.get("skipped"):
        response["skipped_count"] = len(raw["skipped"]) if isinstance(raw["skipped"], list) else 0
    return response


def sync_completed_tasks(root: Path, project: str | None = None,
                        task_dir: str | os.PathLike[str] | None = None,
                        clip: str | None = None, max_tasks: int = 25,
                        mapping_file: str | os.PathLike[str] | None = None) -> dict:
    """Scan terminal video-generator handoffs and hand them to the active project once.

    Only bounded, regular JSON files in the configured handoff inbox are considered.
    In-flight tasks, private task payloads, and files outside the inbox are
    left untouched; the handoff adapter performs the bounded remote cache,
    local output, and privacy checks. When the source skill is available, the fuller
    ``vpm_sync`` scanner also honors safe per-task mappings and records tasks
    that cannot be matched instead of assigning them to the wrong project.
    """

    full_scan = _sync_with_vpm_sync(root, project, task_dir, clip, max_tasks, mapping_file)
    if full_scan is not None:
        return full_scan

    try:
        limit = max(1, min(int(max_tasks), MAX_SCAN_TASKS))
    except (TypeError, ValueError):
        limit = 25
    slug = _active_project(root, project)
    if not slug:
        return {"ok": bool(project is None), "skipped": "no_active_project", "scanned": 0,
                "imported": 0, "idempotent": 0, "pending_count": 0,
                "unmatched_count": 0, "delivery_failed_count": 0,
                "failed": 0, "results": []}
    directory = _task_directory(task_dir)
    if directory is None and not task_dir:
        inbox = root / "inbox"
        if inbox.is_dir() and not inbox.is_symlink():
            directory = inbox.resolve(strict=False)
    if directory is None:
        return {"ok": True, "project": slug, "skipped": "task_dir_unavailable", "scanned": 0,
                "imported": 0, "idempotent": 0, "pending_count": 0,
                "unmatched_count": 0, "delivery_failed_count": 0,
                "failed": 0, "results": []}
    try:
        candidates = [item for item in directory.glob("*.json")
                      if item.is_file() and not item.is_symlink() and not item.name.startswith(".")]
        candidates.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
    except OSError:
        candidates = []
    scanned = imported = idempotent = failed = 0
    unmatched_count = delivery_failed_count = 0
    results: list[dict] = []
    for task_file in candidates[:limit]:
        task = _read_task_snapshot(task_file)
        if task is None or _task_status(task) not in TERMINAL_TASK_STATUSES:
            continue
        scanned += 1
        with sanitized_process_environment():
            outcome = _call_handoff(root, slug, task_file, clip)
        if outcome.get("ok"):
            if outcome.get("idempotent"):
                idempotent += 1
            else:
                imported += 1
        else:
            failed += 1
        if len(results) < 25:
            results.append(outcome)
    unmatched_count, delivery_failed_count = _pending_row_counts(results)
    return {"ok": True, "project": slug, "scanned": scanned, "imported": imported,
            "idempotent": idempotent, "pending_count": failed,
            "unmatched_count": unmatched_count,
            "delivery_failed_count": delivery_failed_count,
            "failed": 0, "results": results}


def write_json_atomic(path: Path, payload: dict) -> None:
    """Write runtime metadata without leaving a partial JSON document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".runtime.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        last_error: OSError | None = None
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                if attempt >= 7:
                    break
                # Windows Defender/indexers can briefly hold the destination
                # after the server has read it.  Retry the atomic swap without
                # falling back to a non-atomic overwrite.
                time.sleep(0.05 * (attempt + 1))
        if last_error is not None:
            raise last_error
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


@contextlib.contextmanager
def ensure_lock(lock_path: Path, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
    """Serialize concurrent ensure calls with a small cross-platform lock file."""

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()}\n".encode("ascii", "replace"))
            os.close(fd)
            fd = -1  # marker: the descriptor is already closed
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > LOCK_STALE_AFTER:
                    lock_path.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("another manager startup is still in progress")
            time.sleep(0.05)
        except OSError as exc:
            raise RuntimeError(f"unable to create startup lock: {exc}") from exc
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _terminate_recorded(pid: object) -> bool:
    """Ask a previously recorded manager process to stop, if it is valid."""

    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0 or value == os.getpid():
        return False
    if os.name == "nt":
        # ``py.exe`` can remain as the parent of the actual Python server.
        # Terminate the recorded process tree so upgrades do not leave the
        # child listening on the port with an old bundle.
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(value), "/T", "/F"],
                capture_output=True,
                timeout=8,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        os.kill(value, signal.SIGTERM)
        return True
    except (ProcessLookupError, ValueError, PermissionError, OSError):
        return False


def _wait_for_state(port: int, bind: str, healthy: bool, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe(port, bind=bind) is healthy:
            return True
        time.sleep(0.1)
    return probe(port, bind=bind) is healthy


def _install_bundle(webdir: Path, upgrade: bool) -> bool:
    changed = False
    webdir.mkdir(parents=True, exist_ok=True)
    for name in RETIRED_BUNDLE_FILES:
        retired = webdir / name
        if retired.is_file() and not retired.is_symlink():
            try:
                retired.unlink()
                changed = True
            except OSError:
                pass
    for name in BUNDLE:
        source = HERE / name
        if name in {"vpm_privacy.py", "vpm_context.py", "vpm_sync.py", "handoff.py", "handoff_core.py", "receive.py", "vpm_receive.py", "handoff_impl.py", "vpm_record.py", "vpm_prepare.py"}:
            source = HERE.parent.parent / "scripts" / name
            # The source skill keeps bookkeeping modules beside ``scripts/``;
            # an installed manager runtime keeps the same files beside
            # ``webapp/``.  Prefer the source copy when it exists, otherwise
            # fall back to the bundled runtime copy for upgrades performed
            # after the original skill directory is no longer available.
            if not source.is_file():
                source = HERE / name
        destination = webdir / name
        # Running the installed copy in-place is common; avoid copying a file
        # onto itself (which is especially important on Windows).
        if source.resolve() == destination.resolve():
            continue
        try:
            differs = (
                source.is_file()
                and (not destination.is_file()
                     or not filecmp.cmp(source, destination, shallow=False))
            )
        except OSError:
            differs = source.is_file()
        if source.is_file() and (upgrade or differs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            changed = True
    return changed


def _spawn(root: Path, webdir: Path, port: int, bind: str,
           task_dir: str | os.PathLike[str] | None = None,
           mapping_file: str | os.PathLike[str] | None = None) -> subprocess.Popen:
    log_path = webdir / "server.log"
    script_args = [
        str(webdir / "server.py"),
        "--root",
        str(root),
        "--port",
        str(port),
        "--bind",
        bind,
        # The manager owns the root application path on its fixed port.
        "--preview-base",
        "/",
    ]
    if task_dir:
        script_args.extend(["--task-dir", str(task_dir)])
    if mapping_file:
        script_args.extend(["--map", str(mapping_file)])
    commands: list[list[str]] = []

    def add_command(command: list[str]) -> None:
        """Append a distinct interpreter command, preserving fallback order."""

        if not command:
            return
        executable = command[0]
        try:
            key = os.path.normcase(os.path.abspath(executable))
        except (OSError, TypeError, ValueError):
            key = str(executable).casefold()
        if any(os.path.normcase(os.path.abspath(item[0])) == key for item in commands):
            return
        commands.append(command)

    def _is_direct_interpreter(value: str) -> bool:
        normalized = value.replace("\\", "/").casefold()
        # LibreOffice's ``python.exe`` is a launcher for python-core and
        # leaves a second process behind.  ``.cmd``/WindowsApps shims have the
        # same PID ambiguity, so they are fallback-only as well.
        return (
            normalized.endswith("/python.exe")
            and "libreoffice" not in normalized
            and "windowsapps" not in normalized
        )

    # Spawn the server with a real Python executable whenever possible.  The
    # Windows ``py.exe`` shim can stay alive as a parent of the actual server,
    # which makes the recorded PID ambiguous and lets an upgrade leave an old
    # watcher behind.  Resolve the interpreter selected by ``py -3`` once,
    # then use direct executables; keep launchers only as final fallbacks.
    if os.name == "nt":
        launcher = shutil.which("py")
        if launcher:
            try:
                resolved = subprocess.run(
                    [launcher, "-3", "-c", "import sys; print(sys.executable)"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    check=False,
                ).stdout.strip().splitlines()[-1]
            except (OSError, subprocess.TimeoutExpired, IndexError):
                resolved = ""
            if resolved and _is_direct_interpreter(resolved):
                add_command([resolved, *script_args])
        for name in ("python", "python3"):
            normal_python = shutil.which(name)
            if normal_python and _is_direct_interpreter(normal_python):
                add_command([normal_python, *script_args])
    if _is_direct_interpreter(sys.executable) or os.name != "nt":
        add_command([sys.executable, *script_args])
    if os.name == "nt":
        launcher = shutil.which("py")
        if launcher:
            add_command([launcher, "-3", *script_args])
    # Keep the child alive after the launcher exits while retaining all output
    # in the private, server-side log (which server.py blocks from static GET).
    log_handle = open(log_path, "ab")
    try:
        process = None
        last_error: OSError | None = None
        for command in commands:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(root),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=manager_child_environment(),
                    start_new_session=True,
                    close_fds=True,
                )
                break
            except OSError as exc:
                last_error = exc
        if process is None:
            assert last_error is not None
            raise last_error
    finally:
        log_handle.close()
    assert process is not None
    return process


def _runtime_payload(root: Path, port: int, bind: str, pid: int | None,
                     started: str | None, started_now: bool) -> dict:
    started_value = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if started_now
        else started
    )
    payload = {
        "name": "video-asset-manager",
        "root": str(root),
        "bind": bind,
        "port": port,
        "pid": pid,
        "health": "/api/health",
        "entry": "/",
        "started": started_value,
        "restart_required_on": ["server.py upgrade only"],
        "note": "data/content changes never require restart; server reads files per request",
    }
    slug = _active_project(root)
    payload["mode"] = "managed-http"
    payload["window"] = {
        "open": True,
        "action": "open_or_focus",
        "port": port,
        "route": f"#/p/{slug}/overview" if slug else "#/",
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ensure the local video-asset-manager runtime is healthy")
    parser.add_argument("command", nargs="?", choices=("start", "sync", "open"), default="start",
                        help="start/probe the runtime; sync also imports completed generator handoffs")
    parser.add_argument("--root", default=None,
                        help="manager data root (overrides VIDEO_ASSET_MANAGER_ROOT/VPM_ROOT)")
    parser.add_argument("--port", type=_valid_port, default=env_port())
    parser.add_argument("--bind", default=env_bind(),
                        help="manager bind address (default: VAM_BIND or 0.0.0.0)")
    parser.add_argument("--task-dir", default=None,
                        help="video-generator handoff inbox (default: VIDEO_GENERATOR_HANDOFF_DIR or the system temp directory)")
    parser.add_argument("--project", default=None,
                        help="project slug for task handoff (default: active_project)")
    parser.add_argument("--clip", default=None,
                        help="optional clip id to use for every imported task")
    parser.add_argument("--max-tasks", type=int, default=25,
                        help="maximum terminal task files to inspect (1-100)")
    parser.add_argument("--map", dest="mapping_file", default=None,
                        help="optional task-to-project JSON map")
    parser.add_argument("--preview", choices=("auto", "always", "never"), default="auto",
                        help="compatibility option; the manager uses port 4200 directly")
    parser.add_argument("--no-preview", dest="no_preview", action="store_true",
                        help="compatibility alias; does not disable the manager port runtime")
    parser.add_argument("--sync", dest="sync_requested", action="store_true",
                        help="scan terminal video-generator handoffs after probing the runtime")
    parser.add_argument("--no-sync", action="store_true",
                        help="skip the automatic task scan")
    parser.add_argument("--upgrade", action="store_true",
                        help="replace the installed webapp bundle and restart the recorded runtime")
    args = parser.parse_args(argv)

    root = resolve_root(args.root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "inbox").mkdir(parents=True, exist_ok=True)
    webdir = root / "webapp"
    runtime_path = webdir / "runtime.json"
    lock_path = webdir / ".ensure.lock"
    bind = str(args.bind or DEFAULT_BIND).strip() or DEFAULT_BIND
    port = int(args.port)
    try:
        with ensure_lock(lock_path):
            bundle_changed = _install_bundle(webdir, args.upgrade)
            effective_upgrade = bool(args.upgrade or bundle_changed)

            previous = read_runtime(runtime_path)
            healthy = probe(port, bind=bind)
            listener = probe_details(port, bind=bind)
            if not healthy and listener.get("port") == "occupied" and listener.get("ownership") == "other":
                print(json.dumps({
                    "ok": False,
                    "error": "port_conflict",
                    "message": f"端口 {port} 已被其他服务占用，未自动终止该进程。",
                    "process": listener.get("process"), "port_state": listener.get("port"),
                    "ownership": listener.get("ownership"), "http": listener.get("http"),
                }, ensure_ascii=False))
                return 1

            # Upgrade means restart only when this launcher has a recorded PID
            # for the requested port. If the port is healthy but no PID is
            # recorded, leave the unknown process alone rather than killing an
            # unrelated service; the next explicit ensure can attach metadata.
            if effective_upgrade and healthy:
                old_port = previous.get("port")
                if old_port is None or str(old_port) == str(port):
                    if _terminate_recorded(previous.get("pid")):
                        if not _wait_for_state(port, bind, False, 8.0):
                            print(json.dumps({
                                "ok": False,
                                "error": "existing manager runtime did not stop for upgrade",
                            }, ensure_ascii=False))
                            return 1
                healthy = probe(port, bind=bind)

            started_now = False
            pid: int | None = None
            if not healthy:
                try:
                    process = _spawn(root, webdir, port, bind, args.task_dir, args.mapping_file)
                except OSError as exc:
                    print(json.dumps({"ok": False, "error": f"unable to start server: {exc}"}, ensure_ascii=False))
                    return 1
                started_now = True
                if not _wait_for_state(port, bind, True, 8.0):
                    # Do not leave a failed child behind. This is a process
                    # started by this invocation, so terminating it is safe.
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    print(json.dumps({
                        "ok": False,
                        "error": "server did not become healthy",
                        "log": str(webdir / "server.log"),
                    }, ensure_ascii=False))
                    return 1
                pid = process.pid
            else:
                try:
                    pid = int(previous.get("pid")) if previous.get("pid") is not None else None
                except (TypeError, ValueError):
                    pid = None

            started = previous.get("started") if isinstance(previous.get("started"), str) else None
            runtime = _runtime_payload(root, port, bind, pid, started, started_now)
            write_json_atomic(runtime_path, runtime)
            do_sync = (
                not args.no_sync
                and (args.sync_requested or args.command in {"start", "sync", "open"})
            )
            sync_result = (
                sync_completed_tasks(
                    root,
                    project=args.project,
                    task_dir=args.task_dir,
                    clip=args.clip,
                    max_tasks=args.max_tasks,
                    mapping_file=args.mapping_file,
                )
                if do_sync
                else {"ok": True, "skipped": "disabled", "scanned": 0,
                      "imported": 0, "idempotent": 0, "failed": 0, "results": []}
            )
            # Keep launcher output useful to the caller while the manifest
            # remains protected by server.py's static-path guard.
            print(json.dumps({"ok": True, "started_now": started_now, **runtime,
                              "process": "running", "port_state": "occupied", "ownership": "self", "http": "healthy",
                              "sync": sync_result}, ensure_ascii=False))
            # Runtime health remains the primary contract for the default
            # start/open path. An explicit `sync` command reports a bad project
            # mapping as a non-zero result so callers can correct it.
            if args.command == "sync" and not sync_result.get("ok", False):
                return 1
            return 0
    except (TimeoutError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
