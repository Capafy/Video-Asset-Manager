#!/usr/bin/env python3
"""Prepare a video project before a generator starts.

This helper is deliberately generator-neutral.  It records the customer's
public input, reserves one clip, and best-effort opens the shared manager
runtime.  Runtime failures are warnings only: the selected video generator
must continue unchanged and can hand the result to ``receive.py`` later.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve()
SCRIPT_DIR = HERE.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import vpm_record  # type: ignore
try:
    import vpm_context  # type: ignore
except Exception:  # pragma: no cover - context continuity is fail-open
    vpm_context = None  # type: ignore

try:
    from vpm_privacy import (  # type: ignore
        contains_private_text,
        manager_child_environment,
    )
except Exception:  # pragma: no cover - compatibility fallback
    def contains_private_text(value: object, *, allow_public_url: bool = False) -> bool:
        text = str(value or "")
        return bool(PRIVATE.search(text)
                    or re.search(r"(?:[A-Za-z]:[\\/]|\\\\|/(?:home|users|tmp|var|private|root|workspace|mnt|opt|srv|etc|run|proc|sys)(?:[\\/]|$))", text, re.I)
                    or (not allow_public_url and re.search(r"(?:https?|s3|file|data|ftp)://|(?<!\\w)//[^\\s]+", text, re.I)))

    def manager_child_environment() -> dict[str, str]:
        blocked = re.compile(
            r"(?:API[_-]?KEY|ACCESS[_-]?KEY|SECRET|TOKEN|PASSWORD|AUTH|"
            r"CREDENTIAL|PRIVATE[_-]?KEY|SIGNATURE)", re.I,
        )
        return {k: v for k, v in os.environ.items() if not blocked.search(str(k))}


DEFAULT_PORT = 4200
# Preflight runs on the generator's critical path.  The launcher is started in
# the background and observed only for this short bound.  A stalled manager
# process therefore cannot hold the generator; the child continues
# independently and a later explicit ``ensure.py open`` or completion sync can
# finish/verify startup.
RUNTIME_OPEN_TIMEOUT_SECONDS = 2.0
RUNTIME_OPEN_OBSERVE_SECONDS = 0.35
RUNTIME_LOCK_STALE_SECONDS = 180.0
MAX_SCRIPT_BYTES = 4 * 1024 * 1024
MAX_ASSET_BYTES = 2 * 1024 * 1024 * 1024
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
SAFE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PRIVATE = re.compile(
    r"(?:api[_-]?key|access[_-]?key|secret|password|authorization|bearer\s|"
    r"token|credential|signed[_-]?url|provider|stack\s*trace)", re.I,
)
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".svg"}
MEDIA_EXTS = IMAGE_EXTS | {
    ".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi", ".mp3", ".wav", ".m4a", ".aac",
}


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(str(temporary_path), str(path))
    finally:
        try:
            temporary_path.unlink()
        except OSError:
            pass


def atomic_text(path: Path, value: str) -> None:
    """Atomically write a short manager marker without following links."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("manager marker is not writable")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(value)
        os.replace(str(temporary_path), str(path))
    finally:
        try:
            temporary_path.unlink()
        except OSError:
            pass


@contextlib.contextmanager
def prepare_lock(root: Path, timeout: float = 20.0) -> Iterator[None]:
    """Serialize prepare operations without making a stale lock permanent."""

    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".prepare.lock"
    deadline = time.monotonic() + timeout
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()}\n".encode("ascii", "replace"))
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 180:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("another project preparation is in progress")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            lock.unlink()
        except OSError:
            pass


def resolve_root(explicit: str | None) -> Path:
    if explicit and explicit.strip():
        return Path(os.path.abspath(os.path.expanduser(explicit.strip())))
    for name in ("VIDEO_ASSET_MANAGER_ROOT", "VPM_ROOT"):
        value = os.environ.get(name)
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
        return Path(os.path.abspath(os.path.join(os.path.expanduser(workspace.strip()), ".capafy", "video-asset-manager")))
    return Path.home() / "workspace" / ".capafy" / "video-asset-manager"


def public_text(value: object, label: str, limit: int = 400_000) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if not text or len(text) > limit or contains_private_text(text):
        raise ValueError(f"{label} is not public input")
    return text


def safe_id(value: object, prefix: str, digest: str) -> str:
    text = str(value or "").strip()
    if text:
        if not SAFE_ID.fullmatch(text) or PRIVATE.search(text):
            raise ValueError("request id is invalid")
        return text
    return f"{prefix}_{digest[:24]}"


def slug_for(title: str, digest: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    if not base:
        base = "video"
    return f"{base[:36]}-{digest[:12]}"


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def approved_file(raw: object, *, label: str, max_bytes: int) -> Path:
    if not isinstance(raw, (str, os.PathLike)):
        raise ValueError(f"{label} must be a file path")
    path = Path(os.fspath(raw)).expanduser().resolve(strict=False)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} is not an available file")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"{label} is not readable") from exc
    if size < 0 or size > max_bytes:
        raise ValueError(f"{label} is too large")
    return path


def parse_assets(values: list[object]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in values:
        if isinstance(raw, dict):
            source = raw.get("file", raw.get("path"))
            name = raw.get("name")
            kind = raw.get("kind")
        else:
            source, name, kind = raw, None, None
        path = approved_file(source, label="asset", max_bytes=MAX_ASSET_BYTES)
        suffix = path.suffix.casefold()
        if suffix not in MEDIA_EXTS:
            raise ValueError("asset type is not supported")
        digest = digest_file(path)
        inferred = "image" if suffix in IMAGE_EXTS else ("audio" if suffix in {".mp3", ".wav", ".m4a", ".aac"} else "video")
        result.append({
            "source": path,
            "name": str(name or path.name)[:160],
            "kind": str(kind or inferred),
            "digest": digest,
        })
    # Preserve order while collapsing duplicate content.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in result:
        if item["digest"] in seen:
            continue
        seen.add(item["digest"])
        unique.append(item)
    return unique


def normalize_request(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize the small public preflight vocabulary for all callers."""

    data = dict(data)
    aliases = {
        "title": ("title", "project_title", "name"),
        "type": ("type", "project_type"),
        "task_id": ("task_id", "task", "id"),
        "request_id": ("request_id", "manager_request_id"),
        "script": ("script", "input_script", "public_script", "public_input"),
        "script_file": ("script_file", "input_script_file", "public_script_file"),
        "duration": ("duration", "duration_seconds"),
        "ratio": ("ratio", "aspect_ratio"),
        "project_slug": ("project_slug", "project"),
        "clip_id": ("clip_id", "clip"),
    }
    for target, names in aliases.items():
        if data.get(target) is not None:
            continue
        for name in names:
            if data.get(name) is not None:
                data[target] = data[name]
                break
    if not data.get("assets"):
        for name in ("media", "input_assets", "images"):
            value = data.get(name)
            if value:
                data["assets"] = value if isinstance(value, list) else [value]
                break
    return data


def load_request(args: argparse.Namespace) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if args.request:
        request_path = Path(args.request).expanduser()
        if str(args.request) == "-":
            raw = sys.stdin.read()
        else:
            raw = request_path.read_text(encoding="utf-8")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("request must be a JSON object")
        data.update(value)

    data = normalize_request(data)
    def choose(name: str, current: object) -> None:
        if current is not None:
            data[name] = current
    choose("title", args.title)
    choose("type", args.type)
    choose("task_id", args.task_id)
    choose("request_id", args.request_id)
    choose("script", args.script)
    choose("script_file", args.script_file)
    choose("duration", args.duration)
    choose("ratio", args.ratio)
    choose("project_slug", args.project_slug)
    choose("clip_id", args.clip_id)
    if args.asset:
        data["assets"] = [*(data.get("assets") or []), *args.asset]
    return data


def map_load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"tasks": {}}
    if not isinstance(value, dict):
        return {"tasks": {}}
    tasks = value.get("tasks")
    return {"tasks": dict(tasks) if isinstance(tasks, dict) else {}}


def _pending_runtime() -> dict[str, Any]:
    """Describe a detached startup without exposing host/process details."""
    return {
        "ok": True,
        "pending": True,
        "warning": "manager startup continues in background",
        "mode": "managed-http",
        "port": DEFAULT_PORT,
        "window": {"open": True, "action": "open_or_focus", "port": DEFAULT_PORT},
    }


def _startup_lock_active(root: Path) -> bool:
    """Avoid stacking detached ensure processes during one cold startup."""

    lock = root / "webapp" / ".ensure.lock"
    try:
        return lock.is_file() and time.time() - lock.stat().st_mtime <= RUNTIME_LOCK_STALE_SECONDS
    except OSError:
        return False


def _runtime_metadata_matches(root: Path) -> bool:
    """Verify a fast ensure exit belongs to this manager data root."""

    metadata = root / "webapp" / "runtime.json"
    try:
        value = json.loads(metadata.read_text(encoding="utf-8"))
        recorded = value.get("root") if isinstance(value, dict) else None
        return bool(recorded and Path(str(recorded)).resolve() == root.resolve())
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError, ValueError):
        return False


def _ensure_launcher(root: Path) -> Path | None:
    """Locate the launcher from either the source skill or installed bundle."""

    here = HERE
    candidates = (
        here.parent / "ensure.py",                         # installed webapp
        here.parent.parent / "assets" / "webapp" / "ensure.py",  # source skill
        root / "webapp" / "ensure.py",                    # copied runtime
    )
    for candidate in candidates:
        try:
            if candidate.is_file() and not candidate.is_symlink():
                return candidate.resolve()
        except (OSError, RuntimeError, ValueError):
            continue
    return None


def _python_command() -> list[str]:
    """Choose a usable interpreter, including the LibreOffice bundled Python."""

    candidate = str(sys.executable or "").strip()
    normalized = candidate.replace("\\", "/").casefold()
    if candidate and (os.name != "nt" or (
        Path(candidate).is_file()
        and normalized.endswith("/python.exe")
        and "libreoffice" not in normalized
        and "windowsapps" not in normalized
    )):
        return [candidate]
    if os.name == "nt":
        launcher = shutil.which("py")
        if launcher:
            return [launcher, "-3"]
    for name in ("python", "python3"):
        found = shutil.which(name)
        if not found:
            continue
        found_normalized = found.replace("\\", "/").casefold()
        if os.name == "nt" and ("libreoffice" in found_normalized
                                 or "windowsapps" in found_normalized):
            continue
        return [found]
    return [candidate or "python"]


def _reap_runtime_launcher(process: subprocess.Popen) -> None:
    """Reap the detached ensure child when the host remains alive (POSIX)."""

    try:
        process.wait()
    except (OSError, ValueError):
        pass


def open_runtime(root: Path, slug: str) -> dict[str, Any]:
    """Best-effort, fail-open runtime startup; never raises into the generator.

    ``ensure.py`` can spend several seconds installing the manager bundle or
    starting the allocated application port. Running it synchronously here
    would put that infrastructure latency on the generator's critical path.
    Start it as a detached child, observe only a short bounded window for an
    immediate result, and return a pending launch when it is still working.
    """

    ensure = _ensure_launcher(root)
    if ensure is None:
        return {"ok": False, "warning": "manager launcher unavailable"}
    if _startup_lock_active(root):
        return _pending_runtime()

    command = [*_python_command(), str(ensure), "open", "--root", str(root), "--project", slug, "--no-sync"]
    popen_kwargs: dict[str, Any] = {
        "cwd": str(ensure.parent),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": manager_child_environment(),
        "start_new_session": True,
        "close_fds": True,
    }
    if os.name == "nt":
        # Do not flash a console window for the best-effort background launch.
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if no_window:
            popen_kwargs["creationflags"] = no_window
    try:
        process = subprocess.Popen(command, **popen_kwargs)
    except (OSError, ValueError):
        return {"ok": False, "warning": "manager runtime could not be started"}

    deadline = time.monotonic() + min(RUNTIME_OPEN_OBSERVE_SECONDS, RUNTIME_OPEN_TIMEOUT_SECONDS)
    while time.monotonic() < deadline:
        try:
            returncode = process.poll()
        except (OSError, ValueError):
            return {"ok": False, "warning": "manager runtime could not be started"}
        if returncode is not None:
            if returncode != 0:
                return {"ok": False, "warning": "manager runtime is unavailable"}
            # The ensure contract guarantees health before a zero exit, but a
            # pre-existing manager may own the fixed port for another root.
            # Require matching private runtime metadata before claiming that
            # this project's window is ready.
            if not _runtime_metadata_matches(root):
                return {"ok": False, "warning": "manager runtime belongs to another workspace"}
            return {"ok": True, "mode": "managed-http",
                    "port": DEFAULT_PORT, "started_now": True}
        time.sleep(0.02)

    # The child is intentionally left alive.  A daemon reaper prevents a
    # zombie on POSIX hosts while preserving the fail-open behavior if the
    # caller exits immediately after preflight.
    try:
        threading.Thread(target=_reap_runtime_launcher, args=(process,),
                         name="video-asset-manager-launcher", daemon=True).start()
    except (RuntimeError, OSError):
        pass
    return _pending_runtime()


def prepare(data: dict[str, Any], root: Path, *, open_window: bool = True) -> dict[str, Any]:
    data = normalize_request(data)
    title = public_text(data.get("title") or "Untitled video", "title", 160)
    ptype = str(data.get("type") or "generate").strip().lower()
    if ptype not in {"generate", "clone"}:
        raise ValueError("type must be generate or clone")
    script_text: str | None = None
    script_source: Path | None = None
    if data.get("script") is not None:
        script_text = public_text(data.get("script"), "script")
    elif data.get("script_file"):
        script_source = approved_file(data.get("script_file"), label="script", max_bytes=MAX_SCRIPT_BYTES)
        try:
            script_text = public_text(script_source.read_text(encoding="utf-8"), "script")
        except (OSError, UnicodeError) as exc:
            raise ValueError("script file is not readable") from exc
    raw_assets = data.get("assets")
    if raw_assets is None:
        raw_assets = []
    elif isinstance(raw_assets, (str, os.PathLike, dict)):
        raw_assets = [raw_assets]
    elif not isinstance(raw_assets, (list, tuple)):
        raise ValueError("assets must be a list of file paths")
    assets = parse_assets(list(raw_assets))
    ratio = str(data.get("ratio") or "9:16").strip()[:32]
    duration = data.get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("duration is invalid") from exc
    if duration is not None and not 0 < duration <= 3600:
        raise ValueError("duration is invalid")

    # The request fingerprint excludes absolute source paths and uses content
    # digests, so the same public request is stable across hosts.
    fingerprint = {
        "title": title, "type": ptype, "script": script_text or "",
        "assets": [{"digest": item["digest"], "kind": item["kind"], "name": item["name"]} for item in assets],
        "duration": duration, "ratio": ratio,
    }
    fingerprint_bytes = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request_digest = hashlib.sha256(fingerprint_bytes).hexdigest()
    request_id = safe_id(data.get("request_id") or data.get("task_id"), "req", request_digest)
    task_id = safe_id(data.get("task_id") or request_id, "task", request_digest)
    slug = str(data.get("project_slug") or "").strip()
    if slug and not SAFE_SLUG.fullmatch(slug):
        raise ValueError("project slug is invalid")
    slug = slug or slug_for(title, request_digest)
    clip_id = str(data.get("clip_id") or f"clip_{request_digest[:12]}").strip()
    if not SAFE_ID.fullmatch(clip_id):
        raise ValueError("clip id is invalid")

    # Read the previous project's edit delta before this preflight changes any
    # state.  This is deliberately best-effort: the generator remains the
    # owner of production and must continue if logs are unavailable/corrupt.
    model_context: dict[str, Any] = {
        "schema": 1,
        "project_slug": slug,
        "summary": {},
        "cursor": {"next_line": 0},
        "ok": True,
    }
    context_warning: str | None = None
    context_slug = slug
    if not data.get("project_slug"):
        # Continuations often arrive with a fresh task id but the same human
        # project title.  Reuse the active project's delta only when its public
        # title matches; never leak edits from an unrelated project.
        try:
            active = (root / "active_project").read_text(encoding="utf-8").strip()
            active_manifest = root / active / "project.json"
            if active and active_manifest.is_file():
                active_doc = json.loads(active_manifest.read_text(encoding="utf-8"))
                if (isinstance(active_doc, dict)
                        and str(active_doc.get("title") or "").strip() == title):
                    context_slug = active
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
    if vpm_context is not None and (root / context_slug / "project.json").is_file():
        try:
            model_context = vpm_context.read_context(root, context_slug)
            model_context["source"] = "active_project" if context_slug != slug else "project"
        except Exception:  # pragma: no cover - defensive fail-open
            context_warning = "previous edit context unavailable"
            model_context = {
                "schema": 1,
                "project_slug": slug,
                "summary": {},
                "cursor": {"next_line": 0},
                "ok": False,
            }

    root.mkdir(parents=True, exist_ok=True)
    with prepare_lock(root):
        mapping_path = root / ".handoff_map.json"
        mapping = map_load(mapping_path)
        prior = mapping["tasks"].get(task_id)
        if isinstance(prior, dict):
            if str(prior.get("fingerprint") or "") != request_digest:
                raise ValueError("task id already belongs to a different request")
            slug = str(prior.get("project_slug") or slug)
            clip_id = str(prior.get("clip_id") or clip_id)

        project = vpm_record.P(str(root), slug)
        existing = Path(project.dir).is_dir() and Path(project.pj).is_file()
        if existing:
            document = project.load()
            request_meta = document.get("manager_request") if isinstance(document.get("manager_request"), dict) else {}
            old_fingerprint = str(request_meta.get("fingerprint") or "")
            if old_fingerprint and old_fingerprint != request_digest:
                raise ValueError("project slug already belongs to a different request")
        else:
            vpm_record.ensure_dirs(project.dir)
            document = vpm_record.empty_project(slug, title, ptype, ratio)

        # Copy public script and assets only after the project boundary exists.
        script_rel: str | None = None
        if script_text is not None:
            suffix = script_source.suffix.lower() if script_source else ".md"
            if suffix not in {".md", ".txt", ".json", ".csv"}:
                suffix = ".md"
            script_rel = f"assets/scripts/input-{request_digest[:12]}{suffix}"
            destination = Path(project.dir) / script_rel
            if not destination.exists():
                destination.write_text(script_text, encoding="utf-8", newline="\n")
        copied_assets: list[dict[str, Any]] = []
        for item in assets:
            suffix = item["source"].suffix.lower()
            filename = re.sub(r"[^A-Za-z0-9._-]+", "_", item["source"].stem).strip("._") or "asset"
            rel = f"assets/uploads/{filename}-{item['digest'][:12]}{suffix}"
            destination = Path(project.dir) / rel
            if not destination.exists():
                shutil.copy2(item["source"], destination)
            copied_assets.append({**item, "file": rel})

        clips = document.setdefault("clips", [])
        clip = next((entry for entry in clips if isinstance(entry, dict) and entry.get("id") == clip_id), None)
        if clip is None:
            clip = {
                "id": clip_id, "title": title, "segment": None,
                "duration": duration, "ratio": ratio, "clarity": "Standard",
                "mappings": [], "preserve": None, "versions": [], "current": None,
                "poster": None, "proxy": None, "filmstrip": None,
                "revision_note": None, "outcome": None, "status": "generating",
                "handoff_status": None, "handoff_message": None,
                "handoff_task_id": None, "handoff_updated": None,
                "request_id": request_id,
            }
            clips.append(clip)
        else:
            clip.setdefault("request_id", request_id)
            if clip.get("request_id") != request_id:
                raise ValueError("clip id already belongs to a different request")
            if clip.get("status") not in {"delivered", "superseded"}:
                clip["status"] = "generating"
                # A fresh preflight is a new attempt by the caller.  Clear a
                # prior transport warning while retaining any delivered
                # version on clips that are already complete.
                clip["handoff_status"] = None
                clip["handoff_message"] = None
                clip["handoff_task_id"] = None
                clip["handoff_updated"] = None
                clip["revision_note"] = None
        document["clips"] = clips
        document["asset"]["clips"] = clips
        if script_rel:
            scripts = document["asset"].setdefault("scripts", [])
            script_id = vpm_record.stable_id("scr", script_rel, "script")
            scripts[:] = [entry for entry in scripts if entry.get("id") != script_id]
            scripts.append({
                "id": script_id, "role": "script", "file": script_rel,
                "name": "Original public input", "format": Path(script_rel).suffix.lstrip("."),
                "related_clips": [clip_id], "status": "active", "created": now(),
            })
        media = document["asset"].setdefault("media", [])
        for item in copied_assets:
            asset_id = vpm_record.stable_id("as", item["file"])
            media[:] = [entry for entry in media if entry.get("id") != asset_id]
            media.append({
                "id": asset_id, "kind": item["kind"], "file": item["file"],
                "name": item["name"], "origin": "upload", "group": "input",
                "tags": ["input"], "poster": None, "used_by": [clip_id],
                "status": "active", "hash": "sha256:" + item["digest"],
            })
        document["asset"]["media"] = media
        document["assets"] = media
        document["assembly"].setdefault("order", [])
        if clip_id not in document["assembly"]["order"]:
            document["assembly"]["order"].append(clip_id)
        document["status"] = "generating"
        document["manager_request"] = {
            "request_id": request_id, "task_id": task_id,
            "fingerprint": request_digest, "clip_id": clip_id,
            "created": document.get("manager_request", {}).get("created", now()) if isinstance(document.get("manager_request"), dict) else now(),
            "status": "prepared",
        }
        event = "project.prepared" if not existing else "project.preflight_reused"
        project.save(document, event, {"task_id": task_id, "clip": clip_id})
        project.state(phase="generating", active_clip=clip_id,
                      queue=[{"clip": clip_id, "status": "generating", "task_id": task_id, "started": now()}])
        task_summary = {
            "schema": 1, "kind": "manager_preflight", "request_id": request_id,
            "task_id": task_id, "project_slug": slug, "clip_id": clip_id,
            "fingerprint": request_digest, "status": "prepared", "created": now(),
            "script_file": script_rel,
            "assets": [{"file": item["file"], "kind": item["kind"], "name": item["name"], "hash": "sha256:" + item["digest"]} for item in copied_assets],
        }
        atomic_json(Path(project.dir) / "tasks" / f"{task_id}.json", task_summary)
        mapping["tasks"][task_id] = {
            "project_slug": slug, "clip_id": clip_id,
            "request_id": request_id, "fingerprint": request_digest,
        }
        atomic_json(mapping_path, mapping)
        active = root / "active_project"
        atomic_text(active, slug + "\n")

    if open_window:
        try:
            runtime = open_runtime(root, slug)
        except Exception:
            # The manager window is a convenience.  A launcher/runtime
            # exception must never turn a successfully persisted preflight
            # into a generator-blocking failure.
            runtime = {"ok": False, "warning": "manager runtime unavailable"}
    else:
        runtime = {"ok": True, "skipped": "disabled"}
    result: dict[str, Any] = {
        "ok": True, "request_id": request_id, "task_id": task_id,
        "project_slug": slug, "clip_id": clip_id, "status": "generating",
        "route": f"#/p/{slug}/overview",
        "window": {
            "open": True,
            "action": "open_or_focus",
            "port": int(runtime.get("port") or DEFAULT_PORT),
            "route": f"#/p/{slug}/overview",
        },
        "runtime": runtime,
        "model_context": model_context,
        "model_context_handoff": {
            "required": bool(model_context.get("summary")),
            "project_slug": str(model_context.get("project_slug") or context_slug),
            "cursor": model_context.get("cursor", {}),
            "ack_command": "vpm_context.py ack after the host confirms context delivery",
        },
    }
    warning = runtime.get("warning")
    if isinstance(warning, str) and warning.strip():
        # A detached/pending launcher is informational only.  Keep the
        # preflight successful so the generator is never vetoed by UI startup.
        result["manager_warning"] = warning[:240]
    elif not runtime.get("ok"):
        result["manager_warning"] = "manager runtime unavailable"
    if context_warning:
        result.setdefault("manager_warnings", []).append(context_warning)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a video project before generation")
    parser.add_argument("command", nargs="?", choices=("prepare", "preflight", "intake"), default="prepare")
    parser.add_argument("--request", help="JSON request file, or - for stdin")
    parser.add_argument("--title")
    parser.add_argument("--type", choices=("generate", "clone"), default=None)
    parser.add_argument("--task-id")
    parser.add_argument("--request-id")
    parser.add_argument("--script", "--input-script", "--public-script", dest="script")
    parser.add_argument("--script-file", "--input-script-file", "--public-script-file", dest="script_file")
    parser.add_argument("--asset", "--asset-file", action="append", default=[])
    parser.add_argument("--duration", "--duration-seconds", dest="duration", type=float)
    parser.add_argument("--ratio", "--aspect-ratio", dest="ratio")
    parser.add_argument("--project-slug", "--project", dest="project_slug")
    parser.add_argument("--clip-id", "--clip", dest="clip_id")
    parser.add_argument("--root")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = load_request(args)
        result = prepare(data, resolve_root(args.root), open_window=not args.no_open)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        # Preparation is a fail-open companion.  A caller can continue its
        # generator after inspecting this warning, but malformed input is
        # still reported deterministically for correction/retry.
        print(json.dumps({"ok": False, "manager_warning": str(exc)[:240]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
