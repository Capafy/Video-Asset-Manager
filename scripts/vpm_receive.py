#!/usr/bin/env python3
"""Generic video result receiver for ``video-asset-manager``.

This command is the manager-side bridge for generators that cannot (or should
not) import this skill.  It accepts a handoff JSON, a single media file, or a
result directory, writes a short sanitized handoff into a private temporary
directory, starts/probes the manager runtime, and delegates registration to
``vpm_sync``.  The source task is never rewritten and the temporary handoff is
removed after the scan.

Examples::

    python vpm_receive.py notify C:/outputs/result.json
    python vpm_receive.py receive C:/outputs/render.mp4 --title "Demo"
    python vpm_receive.py watch C:/outputs --interval 2
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    import handoff_core as handoff_module  # type: ignore[import-not-found]  # noqa: E402
except Exception:  # pragma: no cover - compatibility with old installs
    import handoff_impl as handoff_module  # noqa: E402
import vpm_sync  # noqa: E402

# Keep the compatibility sanitizer below aligned with the scanner's shared
# patterns.  Older bundles may not ship ``vpm_privacy.py`` yet, but the
# scanner still exposes these legacy names.
PRIVATE_RE = getattr(vpm_sync, "PRIVATE_RE", re.compile(
    r"(?:api[_-]?key|apikey|secret|password|authorization|bearer\s|"
    r"access[_-]?token|refresh[_-]?token|private[_-]?key|signed[_-]?url|"
    r"provider|model[_-]?route|stack\s*trace)", re.I,
))
ABSOLUTE_RE = getattr(vpm_sync, "ABSOLUTE_RE", re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\|/(?:home|users|tmp|var|private|root|workspace|mnt|opt|srv|etc|run|proc|sys)(?:[\\/]|$))",
    re.I,
))

try:
    from vpm_privacy import (  # type: ignore[import-not-found]  # noqa: E402
        redact_recursive,
        redact_remote_reference,
        sanitize_public_text,
    )
except Exception:  # pragma: no cover - compatibility with old installs
    def sanitize_public_text(value: object, *, limit: int = 4000,
                             fallback: str | None = None,
                             allow_public_url: bool = False) -> str | None:
        text = str(value or "").replace("\x00", "").strip()
        if not text or len(text) > limit:
            return fallback
        if PRIVATE_RE.search(text) or ABSOLUTE_RE.search(text):
            return fallback
        if not allow_public_url and re.search(r"(?:https?|s3|file|data|ftp)://|(?<!\w)//[^\s]+", text, re.I):
            return fallback
        return text

    def redact_remote_reference(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        return re.sub(r"[?#].*$", "", value.strip()) or None

    def redact_recursive(value: object, *, depth: int = 0) -> object:
        if depth > 12:
            return None
        if isinstance(value, dict):
            result: dict[str, object] = {}
            for key, child in value.items():
                if PRIVATE_RE.search(str(key)):
                    continue
                clean = redact_recursive(child, depth=depth + 1)
                if clean is not None:
                    result[str(key)] = clean
            return result
        if isinstance(value, list):
            return [clean for child in value
                    if (clean := redact_recursive(child, depth=depth + 1)) is not None]
        if isinstance(value, str):
            return sanitize_public_text(value, limit=2_000_000)
        return value if isinstance(value, (int, float, bool)) or value is None else None


DEFAULT_PORT = 4200
DEFAULT_BIND = "0.0.0.0"
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_DISCOVERED_FILES = 1000
MAX_VIDEO_CANDIDATES = 100
VIDEO_EXTS = set(handoff_module.VIDEO_EXTS)
REPORT_EXTS = {".md", ".markdown", ".txt", ".json", ".jsonl"}
# Public generator input is stored as a script-chain resource.  Keep this
# allow-list intentionally textual; binary/provider task payloads must never
# cross the handoff boundary as a "script".
SCRIPT_EXTS = set(getattr(handoff_module, "SCRIPT_EXTS", {
    ".md", ".markdown", ".txt", ".text", ".json", ".jsonl", ".yaml",
    ".yml", ".srt", ".vtt", ".csv",
}))
SCRIPT_INLINE_KEYS = tuple(getattr(handoff_module, "SCRIPT_INLINE_KEYS", (
    "input_script", "public_script", "public_input",
)))
SCRIPT_FILE_KEYS = tuple(getattr(handoff_module, "SCRIPT_FILE_KEYS", (
    "input_script_file", "public_script_file",
)))
SCRIPT_META_KEYS = {
    "script_role": ("input_script_role", "public_script_role"),
    "script_name": ("input_script_name", "public_script_name"),
    "script_format": ("input_script_format", "public_script_format"),
}
# A bare ``prompt`` field is intentionally not accepted: it may contain the
# generator's private transformed prompt. Producers must use the explicit
# ``input_script``/``public_script`` field for a customer-visible input.
SCRIPT_ROLE_VALUES = {"script", "storyboard", "subtitle", "analysis_report"}
MAX_SCRIPT_BYTES = 2 * 1024 * 1024

# These are the only task fields copied into the ephemeral handoff.  The
# downstream handoff adapter performs a second privacy pass before persistence.
PUBLIC_TEXT_KEYS = (
    "project_title", "title", "name", "summary", "message", "user_summary",
)
PUBLIC_ID_KEYS = (
    "task_id", "id", "project_slug", "manager_project", "video_project",
    "capafy_project_slug", "project", "clip_id", "manager_clip_id", "clip",
    "segment_id",
    # A generator may allocate a provider task id after manager preflight.
    # Preserve these stable manager-side aliases so vpm_sync can resolve the
    # reservation map without coupling the receiver to any one generator.
    "manager_task_id", "preflight_task_id", "prepared_task_id",
    "manager_request_id", "request_id",
)
PUBLIC_STATUS_KEYS = (
    "status", "task_status", "generation_status", "upstream_terminal_status",
    "upstream_last_video_status", "video_status", "delivery_status",
    "failure_stage", "failure_reason", "error", "generation_completion_confirmed",
)
REMOTE_KEYS = (
    "video_url", "videoUrl", "download_url", "downloadUrl", "file_url", "fileUrl",
    "object_url", "objectUrl", "s3_url", "s3Url", "video_uri", "videoUri",
    "resource_url", "resourceUrl", "download_link", "downloadLink", "object_key",
    "objectKey", "file_key", "fileKey", "resource_key", "resourceKey",
)


class ReceiveError(RuntimeError):
    """A user-visible, sanitized receive failure."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "\x1f".join(str(item or "") for item in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def _safe_text(value: object, limit: int = 500) -> str | None:
    cleaned = handoff_module.clean_public(value, limit=limit, fallback=None)
    return cleaned if isinstance(cleaned, str) and cleaned else None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ReceiveError("结果文件不可用。")
        if path.stat().st_size <= 0 or path.stat().st_size > MAX_JSON_BYTES:
            raise ReceiveError("结果文件不可用。")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ReceiveError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ReceiveError("结果文件不是有效 JSON。")
    if not isinstance(value, dict):
        raise ReceiveError("结果文件不是有效 JSON。")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ReceiveError("接收目录中的目标文件不可用。")
    except OSError as exc:
        raise ReceiveError("接收目录中的目标文件不可用。") from exc
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _path_inside(base: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath((str(base.resolve(strict=False)), str(candidate.resolve(strict=False)))) == str(base.resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return False


def _existing_file(path: Path, *, base: Path | None = None) -> Path | None:
    try:
        if path.is_symlink():
            return None
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            return None
        if base is not None and not _path_inside(base, resolved):
            return None
        return resolved
    except (OSError, RuntimeError):
        return None


def _copy_bounded(source: Path, destination: Path, *, max_bytes: int, minimum: int = 1) -> Path:
    resolved = _existing_file(source)
    if resolved is None:
        raise ReceiveError("结果文件不可用。")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ReceiveError("结果文件不可用。") from exc
    if size < minimum or size > max_bytes:
        raise ReceiveError("结果文件大小不可用。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffix = resolved.suffix.lower()
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(resolved, temporary)
        os.replace(temporary, destination)
    except OSError as exc:
        raise ReceiveError("结果文件暂存失败。") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return destination


def _safe_task_id(raw: object, fallback: str) -> str:
    value = str(raw or "").strip()
    if vpm_sync.TASK_ID_RE.fullmatch(value) and not vpm_sync.PRIVATE_RE.search(value):
        return value
    return _stable_id("task", fallback)


def _safe_slug(raw: object) -> str | None:
    # Keep the receiver's validation aligned with the scanner, including its
    # reserved workspace names.  Rejecting them here avoids writing a task
    # that can never be routed and prevents a handoff from targeting the
    # manager's own ``webapp``/``trash`` directories.
    return vpm_sync.safe_slug(raw)


def _safe_clip(raw: object) -> str | None:
    return vpm_sync.safe_clip(raw)


def _remote_reference(task: dict[str, Any]) -> str | None:
    try:
        value = handoff_module.remote_video_reference(task)
    except Exception:
        return None
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _local_reference(task: dict[str, Any], task_file: Path) -> Path | None:
    """Resolve a source-local video through the handoff's approved roots."""
    try:
        value, _error = handoff_module.local_video(task, task_file)
    except Exception:
        return None
    return value


def _candidate_task_files(directory: Path) -> list[Path]:
    """Return bounded JSON result candidates from a generator output folder.

    Older versions only accepted four conventional filenames.  That made the
    host-side watcher miss otherwise valid envelopes such as
    ``generation-123.json``.  Keep conventional names first (so a directory
    containing both a provider task dump and a result envelope remains
    deterministic), then allow any regular top-level JSON file.  The receiver
    still applies the status, video, path and privacy checks before importing.
    """
    preferred = {"handoff.json", "result.json", "task.json", "output.json"}
    found: list[tuple[int, Path]] = []
    try:
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".json":
                continue
            if path.name.startswith("."):
                continue
            name = path.name.casefold()
            rank = 0 if (
                name in preferred
                or name.endswith((".handoff.json", ".result.json"))
            ) else 1
            found.append((rank, path))
    except OSError:
        return []
    return [
        path for _rank, path in sorted(
            found, key=lambda item: (item[0], item[1].name.casefold(), item[1].name)
        )
    ]


def _candidate_videos(directory: Path, *, recursive: bool = True) -> list[Path]:
    candidates: list[Path] = []
    try:
        if not recursive:
            iterator: Iterable[Path] = directory.iterdir()
            for path in iterator:
                if len(candidates) >= MAX_VIDEO_CANDIDATES:
                    break
                if path.is_symlink() or not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
                    continue
                candidates.append(path)
        else:
            visited = 0
            for current, dirs, files in os.walk(directory):
                dirs[:] = [name for name in dirs if not name.startswith(".") and name not in {"staging", "tasks", "webapp", "trash"}]
                for name in files:
                    visited += 1
                    if visited > MAX_DISCOVERED_FILES or len(candidates) >= MAX_VIDEO_CANDIDATES:
                        break
                    path = Path(current) / name
                    if path.is_symlink() or path.suffix.lower() not in VIDEO_EXTS:
                        continue
                    candidates.append(path)
                if visited > MAX_DISCOVERED_FILES or len(candidates) >= MAX_VIDEO_CANDIDATES:
                    break
    except OSError:
        return []
    try:
        candidates.sort(key=lambda item: (item.stat().st_mtime_ns, item.name.casefold()), reverse=True)
    except OSError:
        candidates.sort(key=lambda item: item.name.casefold())
    return candidates


def _resolve_override_video(
    value: str | None,
    base: Path,
    *,
    task: dict[str, Any] | None = None,
    source_file: Path | None = None,
    allow_external: bool = False,
) -> Path | None:
    """Resolve an override without letting task JSON choose arbitrary files.

    An explicit ``--video`` supplied by the caller is allowed to point at a
    user-selected local output.  A path discovered inside a result JSON is
    instead constrained to the same approved roots used by the handoff
    adapter (task directory, configured output roots, and workspace inboxes).
    """
    if not value:
        return None
    if not allow_external and task is not None:
        try:
            approved = handoff_module.approved_local_file(
                value, task, source_file, VIDEO_EXTS, allow_video_signature=True,
            )
        except Exception:
            approved = None
        if approved is not None:
            return approved
        # Do not fall through to an unrestricted absolute-path read when a
        # task supplied the value and approved_local_file rejected it.
        return None
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raw = base / raw
    candidate = _existing_file(raw)
    if candidate is None:
        return None
    if allow_external:
        return candidate
    return candidate if _path_inside(base, candidate) else None


def _copy_report(value: object, source_task: dict[str, Any], source_file: Path | None,
                 stage: Path) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = value.strip()
    report_path: Path | None = None
    if source_file is not None:
        try:
            report_path = handoff_module.approved_local_file(
                text, source_task, source_file, REPORT_EXTS,
            )
        except Exception:
            report_path = None
    if report_path is not None:
        try:
            if report_path.stat().st_size > MAX_REPORT_BYTES:
                return None, "分析报告大小不可用。"
        except OSError:
            return None, "分析报告无法读取。"
        destination = stage / f"report{report_path.suffix.lower() or '.md'}"
        # A report file is customer-visible once it reaches the project.  Do
        # not copy the source bytes verbatim: reports produced by a worker can
        # contain absolute paths, signed URLs, provider diagnostics, or
        # credential-shaped fields even when the handoff JSON itself is
        # otherwise clean.  Parse structured reports when possible and fall
        # back to line-level filtering for markdown/plain text.
        try:
            raw_text = report_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None, "分析报告无法读取。"
        cleaned_text = _sanitize_report_text(raw_text, report_path.suffix.lower())
        if not cleaned_text:
            return None, "分析报告未返回。"
        _write_text_atomic(destination, cleaned_text.rstrip() + "\n")
        return destination.name, None
    cleaned = _safe_text(text, limit=2_000_000)
    if cleaned is None:
        return None, "分析报告未返回。"
    destination = stage / "report.md"
    _write_text_atomic(destination, cleaned + "\n")
    return destination.name, None


def _script_alias_value(raw: dict[str, Any]) -> tuple[Any, str | None]:
    """Return (inline value, file reference) for a public input script."""
    for key in SCRIPT_FILE_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return None, value.strip()
    for key in SCRIPT_INLINE_KEYS:
        if key in raw and raw.get(key) is not None:
            return raw.get(key), None
    return None, None


def _script_path_like(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip().replace("\\", "/")
    if not text:
        return False
    lower = text.lower().split("?", 1)[0].split("#", 1)[0]
    return (
        lower.startswith(("file://", "outputs/", "output/", "agent-outputs/"))
        or lower.startswith(("/", "\\"))
        or bool(re.match(r"^[A-Za-z]:/", lower))
        or Path(lower.rsplit("/", 1)[-1]).suffix in SCRIPT_EXTS
    )


def _read_script_override(value: object, source_task: dict[str, Any],
                          source_file: Path | None, base: Path,
                          *, allow_external: bool = False) -> tuple[str | None, str]:
    """Read/sanitize an explicit script override without persisting its path."""
    if value is None:
        return None, ""
    candidate: Path | None = None
    if isinstance(value, str) and value.strip() and _script_path_like(value) and "\n" not in value and "\r" not in value:
        if allow_external:
            raw_path = Path(value).expanduser()
            if not raw_path.is_absolute():
                raw_path = base / raw_path
            candidate = _existing_file(raw_path)
        else:
            try:
                candidate = handoff_module.approved_local_file(
                    value, source_task, source_file, SCRIPT_EXTS,
                )
            except Exception:
                candidate = None
        if candidate is None:
            return None, ""
        try:
            if candidate.stat().st_size > MAX_SCRIPT_BYTES:
                return None, ""
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None, ""
        suffix = candidate.suffix.lower()
    else:
        text = value
        suffix = ""
    try:
        clean = handoff_module.sanitize_script_text(text, suffix)
    except Exception:
        clean = None
    return clean, suffix


def _copy_script(raw: dict[str, Any], source_task: dict[str, Any], source_file: Path | None,
                 stage: Path, *, script_override: object = None,
                 script_file_override: str | None = None,
                 script_override_external: bool = False) -> tuple[str | None, dict[str, str]]:
    """Stage one public input script and return (filename, metadata)."""
    value, file_ref = _script_alias_value(raw)
    if script_file_override:
        value, file_ref = None, script_file_override
    if script_override is not None:
        value, file_ref = script_override, None
    if value is None and file_ref is None:
        return None, {}
    if file_ref is not None:
        value = file_ref
    text, suffix = _read_script_override(
        value, source_task, source_file, source_file.parent if source_file else stage,
        allow_external=script_override_external and script_file_override is not None,
    )
    if not text:
        return None, {}
    role_value = None
    for key in SCRIPT_META_KEYS["script_role"]:
        if raw.get(key) is not None:
            role_value = raw.get(key)
            break
    name_value = None
    for key in SCRIPT_META_KEYS["script_name"]:
        if raw.get(key) is not None:
            name_value = raw.get(key)
            break
    fmt_value = None
    for key in SCRIPT_META_KEYS["script_format"]:
        if raw.get(key) is not None:
            fmt_value = raw.get(key)
            break
    try:
        info = handoff_module.public_script_info(
            text,
            name=name_value,
            role=role_value,
            fmt=fmt_value,
            suffix=suffix,
        ) or {}
    except Exception:
        info = {}
    if not info:
        return None, {}
    extension = suffix or ("." + str(info.get("format") or "md").lower().lstrip("."))
    if extension not in SCRIPT_EXTS:
        extension = ".md"
    filename = f"script{extension}"
    stage.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(stage / filename, text.rstrip() + "\n")
    metadata = {
        "script_role": str(info.get("role") or "script"),
        "script_name": str(info.get("name") or "原始输入脚本"),
        "script_format": str(info.get("format") or extension.lstrip(".")),
    }
    return filename, metadata


def _sanitize_report_text(raw_text: str, suffix: str = "") -> str:
    """Return a bounded public report projection.

    Structured JSON reports are recursively redacted so safe fields survive;
    markdown/text reports are filtered per line so one diagnostic URL does not
    discard the complete user-facing analysis.  The function deliberately
    returns no transport URL or absolute path, including when a report uses a
    non-standard key name.
    """
    if not isinstance(raw_text, str):
        return ""
    if len(raw_text.encode("utf-8", "ignore")) > MAX_REPORT_BYTES:
        return ""
    suffix = (suffix or "").lower()
    if suffix in {".json", ".jsonl"}:
        if suffix == ".json":
            try:
                parsed = json.loads(raw_text)
            except (UnicodeError, json.JSONDecodeError):
                parsed = None
            if parsed is not None:
                clean = redact_recursive(parsed)
                if clean is None:
                    return ""
                try:
                    return json.dumps(clean, ensure_ascii=False, indent=2)
                except (TypeError, ValueError):
                    return ""
        # JSONL is intentionally processed one line at a time.  Invalid lines
        # are treated as ordinary text and still pass through the same public
        # sanitizer rather than being copied raw.
        output: list[str] = []
        for line in raw_text.splitlines():
            if not line.strip():
                continue
            try:
                parsed_line = json.loads(line)
            except (UnicodeError, json.JSONDecodeError):
                clean_line = sanitize_public_text(line, limit=4000, fallback=None)
                if clean_line:
                    output.append(clean_line)
                continue
            clean_line = redact_recursive(parsed_line)
            if clean_line is None:
                continue
            try:
                output.append(json.dumps(clean_line, ensure_ascii=False, separators=(",", ":")))
            except (TypeError, ValueError):
                continue
        return "\n".join(output)

    output_lines: list[str] = []
    for line in raw_text.splitlines():
        # Keep line lengths bounded independently so a malicious single-line
        # report cannot consume the JSON/API response budget.
        clean_line = sanitize_public_text(line, limit=8_000, fallback=None)
        if clean_line:
            output_lines.append(clean_line)
    return "\n".join(output_lines)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _sanitized_task(
    raw: dict[str, Any],
    *,
    source_label: str,
    video_name: str | None,
    report_name: str | None,
    script_name: str | None,
    script_meta: dict[str, str] | None,
    generator: str | None,
    title: str | None,
) -> dict[str, Any]:
    task: dict[str, Any] = {}
    for key in PUBLIC_STATUS_KEYS:
        if key not in raw:
            continue
        value = raw.get(key)
        if isinstance(value, bool) and key == "generation_completion_confirmed":
            task[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            task[key] = value
        elif isinstance(value, str):
            cleaned = _safe_text(value, limit=500)
            if cleaned:
                task[key] = cleaned

    for key in PUBLIC_ID_KEYS:
        if key not in raw:
            continue
        value = raw.get(key)
        if key in {"project_slug", "manager_project", "video_project", "capafy_project_slug", "project"}:
            safe = _safe_slug(value)
        elif key in {"clip_id", "manager_clip_id", "clip", "segment_id"}:
            safe = _safe_clip(value)
        elif key in {
            "task_id", "id", "manager_task_id", "preflight_task_id",
            "prepared_task_id", "manager_request_id", "request_id",
        }:
            # Keep an alias only when it is itself a safe stable identifier.
            # Do not substitute ``source_label`` here: doing so could make an
            # invalid provider field accidentally collide with a preparation
            # map entry.
            candidate = str(value or "").strip()
            safe = (
                candidate
                if vpm_sync.TASK_ID_RE.fullmatch(candidate)
                and not vpm_sync.PRIVATE_RE.search(candidate)
                else None
            )
            if key in {"task_id", "id"} and safe is None:
                safe = _safe_task_id(value, source_label)
        else:
            safe = _safe_text(value, limit=160)
        if safe:
            task[key] = safe

    for key in PUBLIC_TEXT_KEYS:
        if key in raw:
            cleaned = _safe_text(raw.get(key), limit=2000)
            if cleaned:
                task[key] = cleaned

    if generator:
        cleaned_generator = _safe_text(generator, limit=48)
        if cleaned_generator:
            task["generator"] = cleaned_generator
    elif "generator" in raw or "generator_name" in raw:
        cleaned_generator = _safe_text(raw.get("generator") or raw.get("generator_name"), limit=48)
        if cleaned_generator:
            task["generator"] = cleaned_generator
    else:
        task["generator"] = "video-generator"

    if title:
        cleaned_title = _safe_text(title, limit=200)
        if cleaned_title:
            task["project_title"] = cleaned_title

    task_id = _safe_task_id(task.get("task_id") or task.get("id"), source_label)
    task["task_id"] = task_id
    task.pop("id", None)
    task["status"] = str(task.get("status") or "completed").strip().lower()
    if task["status"] not in vpm_sync.TERMINAL_STATUSES:
        # A receiver is invoked at delivery time.  Preserve a known status but
        # keep malformed/empty values from being silently treated as running.
        task["status"] = "completed"
    if video_name:
        task["video_file"] = video_name
    else:
        # The inbox is a private transport queue (and is blocked by the web
        # server).  Preserve only a *query-free* public reference so a later
        # scan can retry an unsigned object.  Signed URLs and arbitrary query
        # parameters are intentionally omitted; the manager must never leave
        # bearer material on disk.  The downstream handoff strips even this
        # redacted hint before writing project manifests, task summaries,
        # logs, or ZIP exports.
        remote_reference = _remote_reference(raw)
        if remote_reference:
            redacted = redact_remote_reference(remote_reference)
            # Keep a plain HTTPS compatibility hint for the private inbox,
            # but never persist S3 object identifiers (bucket/key names can be
            # sensitive even without a query string).  Any query/fragment is
            # considered signed/transport state and is dropped as well.
            parsed_remote = urlparse(redacted) if redacted else None
            if (redacted and redacted == remote_reference
                    and parsed_remote is not None
                    and parsed_remote.scheme.lower() == "https"
                    and not PRIVATE_RE.search(redacted)):
                task["video_url"] = redacted
            task["remote_video_pending"] = True
    if report_name:
        task["report_file"] = report_name
    if script_name:
        task["script_file"] = script_name
        if script_meta:
            # Metadata is kept scalar and separately allow-listed so a
            # structured generator payload cannot smuggle private fields into
            # the inbox task.
            transport_keys = {
                "script_role": "input_script_role",
                "script_name": "input_script_name",
                "script_format": "input_script_format",
            }
            for key, output_key in transport_keys.items():
                value = script_meta.get(key)
                if isinstance(value, str) and value:
                    task[output_key] = value
    if "generation_completion_confirmed" not in task and video_name:
        task["generation_completion_confirmed"] = True
    if "message" not in task:
        task["message"] = "视频结果已接收。"
    return task


def _prepare_handoff(source: Path, stage: Path, *, video_override: str | None = None,
                     video_url_override: str | None = None,
                     generator: str | None = None, title: str | None = None,
                     report_override: str | None = None,
                     script_override: object = None,
                     script_file_override: str | None = None,
                     script_role_override: str | None = None,
                     script_name_override: str | None = None,
                     script_format_override: str | None = None,
                     task_id_override: str | None = None,
                     status_override: str | None = None,
                     message_override: str | None = None,
                     project_override: str | None = None,
                     clip_override: str | None = None,
                     video_override_external: bool = False,
                     script_override_external: bool = False) -> Path:
    # Check the caller-supplied path before resolving it.  Resolving first
    # would erase the symlink bit and allow a symlinked task/result directory
    # to be treated as trusted input.
    source = source.expanduser()
    try:
        if source.is_symlink():
            raise ReceiveError("输入结果不可用。")
    except OSError as exc:
        raise ReceiveError("输入结果不可用。") from exc
    source = source.resolve(strict=False)
    raw: dict[str, Any] = {}
    source_file: Path | None = None
    base = source.parent if source.is_file() else source
    explicit_video: Path | None = None

    if source.is_file() and source.suffix.lower() == ".json":
        source_file = source
        raw = _read_json(source)
    elif source.is_file():
        explicit_video = explicit_video or _existing_file(source)
    elif source.is_dir():
        task_files = _candidate_task_files(source)
        if task_files:
            source_file = task_files[0]
            raw = _read_json(source_file)
        else:
            source_file = None
    else:
        raise ReceiveError("输入结果不存在。")

    # Resolve an explicit override only after the source task has been read so
    # approved_local_file() can enforce its bounded roots.  A direct media
    # source remains accepted through the branch above; only a caller-marked
    # external override may bypass the task-root check.
    explicit_video = _resolve_override_video(
        video_override,
        base,
        task=raw if raw else None,
        source_file=source_file,
        allow_external=video_override_external,
    )

    video = explicit_video
    if video is None and source.is_file() and source.suffix.lower() != ".json":
        # A direct media submission is itself an explicit source and does not
        # need a task-root lookup.
        video = _existing_file(source)
    if video_url_override and not video:
        # Keep remote references in the ephemeral task only.  The handoff
        # adapter may cache them during the scan, but never persists the URL
        # to a project manifest or export.
        raw["video_url"] = video_url_override
    if task_id_override is not None:
        raw["task_id"] = task_id_override
    if status_override is not None:
        raw["status"] = status_override
    if message_override is not None:
        raw["message"] = message_override
    if project_override is not None:
        raw["project_slug"] = project_override
    if clip_override is not None:
        raw["clip_id"] = clip_override
    if script_role_override is not None:
        raw["input_script_role"] = script_role_override
    if script_name_override is not None:
        raw["input_script_name"] = script_name_override
    if script_format_override is not None:
        raw["input_script_format"] = script_format_override
    if video is None and raw and source_file is not None:
        video = _local_reference(raw, source_file)
    if video is None and source.is_dir():
        # A result directory may contain a worker-style virtual path in its
        # JSON while the copied local artifact sits beside the JSON.
        discovered = _candidate_videos(source)
        if discovered:
            video = _existing_file(discovered[0])
    remote_reference = _remote_reference(raw)
    if video is None and remote_reference:
        # Cache while the reference is still in memory.  This keeps the
        # persistent handoff generator-neutral and free of signed URLs, while
        # still supporting HTTPS/S3-only generators and ``--no-sync``.
        task_hint = _safe_task_id(raw.get("task_id") or raw.get("id"), source.name or "remote")
        try:
            cached, _remote_error = handoff_module.cache_remote_video(
                remote_reference, stage / "remote-cache", task_hint,
            )
        except Exception:
            cached = None
        if cached is not None:
            video = cached
    if video is None and raw and remote_reference is None:
        status = str(raw.get("status") or raw.get("task_status") or "").strip().lower()
        if status not in vpm_sync.TERMINAL_STATUSES:
            raise ReceiveError("结果中没有可接收的视频。")

    stage.mkdir(parents=True, exist_ok=True)
    video_name: str | None = None
    if video is not None:
        suffix = video.suffix.lower() if video.suffix.lower() in VIDEO_EXTS else ".mp4"
        video_name = f"video{suffix}"
        _copy_bounded(video, stage / video_name, max_bytes=MAX_VIDEO_BYTES, minimum=1)

    report_value: object = report_override
    if report_value is None:
        for key in ("report", "report_file", "analysis_report"):
            if raw.get(key) is not None:
                report_value = raw.get(key)
                break
    report_name: str | None = None
    if report_value is not None:
        report_name, _report_error = _copy_report(report_value, raw, source_file, stage)

    script_name: str | None = None
    script_meta: dict[str, str] = {}
    script_name, script_meta = _copy_script(
        raw,
        raw,
        source_file,
        stage,
        script_override=script_override,
        script_file_override=script_file_override,
        script_override_external=script_override_external,
    )

    label = source.name or "result"
    # A bare media path has no task JSON from which to derive an identity.
    # Include the content digest in the fallback label so two different
    # direct video submissions cannot collapse into one task merely because
    # both use the temporary ``result.json`` filename.  Re-submitting the
    # same bytes remains idempotent.
    identity = None
    raw_identity = raw.get("task_id") or raw.get("id")
    # A malformed/unsafe task id must not collapse unrelated direct media
    # results onto the same filename-based fallback.  Treat it like a missing
    # id and derive the fallback from the bytes (or the remote reference).
    safe_identity = (
        isinstance(raw_identity, str)
        and bool(vpm_sync.TASK_ID_RE.fullmatch(raw_identity.strip()))
        and not vpm_sync.PRIVATE_RE.search(raw_identity)
    )
    if not safe_identity:
        if video is not None:
            try:
                identity = _digest_file(video)
            except OSError:
                identity = None
        if identity is None:
            remote = _remote_reference(raw)
            if remote:
                identity = hashlib.sha256(remote.encode("utf-8")).hexdigest()
    source_label = _safe_task_id(raw.get("task_id") or raw.get("id"),
                                 f"{label}:{identity}" if identity else label)
    task = _sanitized_task(
        raw,
        source_label=source_label,
        video_name=video_name,
        report_name=report_name,
        script_name=script_name,
        script_meta=script_meta,
        generator=generator,
        title=title,
    )
    task_path = stage / f"{task['task_id']}.json"
    _write_json_atomic(task_path, task)
    return task_path


def _safe_runtime_summary(runtime: object) -> dict[str, Any]:
    """Return only the current manager HTTP runtime contract.

    Runtime summaries are a host handoff boundary.  Keep the fixed manager
    mode and window fields, but never propagate the retired instance/Preview
    envelope even when an older launcher returns it.
    """

    if not isinstance(runtime, dict):
        return {}
    result: dict[str, Any] = {
        key: runtime[key]
        for key in ("started_now", "ok", "mode")
        if key in runtime and isinstance(runtime[key], (bool, int, float, str))
    }
    current_runtime = result.get("mode") == "managed-http"
    if not current_runtime:
        result.pop("mode", None)
    for key in ("port", "health", "entry"):
        value = runtime.get(key)
        if isinstance(value, (bool, int, float, str)):
            result[key] = value
    window = runtime.get("window")
    if isinstance(window, dict):
        safe_window = {}
        for key in ("open", "action", "transport", "port", "route"):
            value = window.get(key)
            if isinstance(value, (bool, int, float, str)):
                safe_window[key] = value
        if safe_window:
            result["window"] = safe_window
    # Read-only compatibility for historical handoff summaries.  The current
    # manager never emits this field; retain it only for an older envelope
    # carrying a validated instance-relative path.
    if not current_runtime:
        legacy = runtime.get("preview")
        if isinstance(legacy, dict):
            path = legacy.get("path")
            if isinstance(path, str) and path.startswith("/instance/"):
                safe_legacy = {
                    key: legacy[key]
                    for key in ("status", "version", "path")
                    if key in legacy and isinstance(legacy[key], (bool, int, float, str))
                }
                if safe_legacy:
                    result["preview"] = safe_legacy
    return result


def _compact_scan(raw: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": bool(raw.get("ok", True))}
    for key in ("scanned", "eligible", "pending_count", "imported", "idempotent", "failed"):
        if key in raw:
            try:
                result[key] = int(raw.get(key) or 0)
            except (TypeError, ValueError):
                pass
    runtime_summary = _safe_runtime_summary(raw.get("runtime"))
    if runtime_summary:
        result["runtime"] = runtime_summary
    warning = raw.get("manager_warning")
    if isinstance(warning, str) and warning.strip():
        result["manager_warning"] = warning[:240]
    received: list[dict[str, Any]] = []
    for item in raw.get("received") or []:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {}
        # Keep the compact receive response useful to a generator host: the
        # public script/report paths are safe project-relative values and make
        # it possible to confirm that the original input was registered.  Do
        # not include any task payload or source paths here.
        for key in ("task_id", "project", "clip_id", "status", "idempotent", "report", "script", "message"):
            value = item.get(key)
            if isinstance(value, (str, bool)):
                row[key] = value[:500] if isinstance(value, str) else value
        outputs = item.get("outputs")
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
        received.append(row)
    result["received"] = received
    pending: list[dict[str, Any]] = []
    for item in raw.get("pending") or []:
        if not isinstance(item, dict):
            continue
        row = {
            key: item[key]
            for key in ("task_id", "project", "status", "reason")
            if isinstance(item.get(key), str)
        }
        pending.append(row)
    result["pending"] = pending[:25]
    if raw.get("skipped"):
        result["skipped_count"] = len(raw["skipped"]) if isinstance(raw["skipped"], list) else 0
    return result


def receive(
    source: str | os.PathLike[str],
    *,
    root: str | os.PathLike[str] | None = None,
    project: str | None = None,
    clip: str | None = None,
    port: int = DEFAULT_PORT,
    bind: str = DEFAULT_BIND,
    mapping_file: str | os.PathLike[str] | None = None,
    video: str | None = None,
    video_url: str | None = None,
    generator: str | None = None,
    title: str | None = None,
    report: str | None = None,
    script: object = None,
    script_file: str | None = None,
    script_role: str | None = None,
    script_name: str | None = None,
    script_format: str | None = None,
    task_id: str | None = None,
    status: str | None = None,
    message: str | None = None,
    video_external: bool = False,
    start_runtime: bool = True,
    use_active_project: bool = True,
) -> dict[str, Any]:
    """Receive one JSON/file/directory and return a sanitized scan result."""
    manager_root = vpm_sync.resolve_root(root)
    manager_root.mkdir(parents=True, exist_ok=True)
    source_path = Path(source).expanduser()
    if not source_path.exists():
        raise ReceiveError("输入结果不存在。")
    mapping = None
    if mapping_file:
        mapping_candidate = Path(mapping_file).expanduser()
        try:
            if mapping_candidate.is_symlink():
                raise ReceiveError("任务映射文件不可用。")
        except OSError as exc:
            raise ReceiveError("任务映射文件不可用。") from exc
        mapping = mapping_candidate.resolve(strict=False)
    with tempfile.TemporaryDirectory(prefix="vam-receive-") as temporary:
        stage = Path(temporary)
        task_path = _prepare_handoff(
            source_path,
            stage,
            video_override=video,
            video_url_override=video_url,
            generator=generator,
            title=title,
            report_override=report,
            script_override=script,
            script_file_override=script_file,
            script_role_override=script_role,
            script_name_override=script_name,
            script_format_override=script_format,
            task_id_override=task_id,
            status_override=status,
            message_override=message,
            project_override=project,
            clip_override=clip,
            video_override_external=video_external,
            # A CLI --script-file is an explicit user-selected input, so it
            # may live outside the task JSON's approved roots.  Paths found
            # inside the result document remain constrained below.
            script_override_external=bool(script_file),
        )
        raw = vpm_sync.scan(
            manager_root,
            stage,
            project=project,
            clip=clip,
            mapping_file=mapping,
            start_runtime=start_runtime,
            port=port,
            bind=bind,
            max_tasks=25,
            use_active_project=use_active_project,
            include_outputs=False,
        )
    return _compact_scan(raw)


def stage_handoff(
    source: str | os.PathLike[str],
    *,
    root: str | os.PathLike[str] | None = None,
    task_dir: str | os.PathLike[str] | None = None,
    project: str | None = None,
    clip: str | None = None,
    video: str | None = None,
    video_url: str | None = None,
    generator: str | None = None,
    title: str | None = None,
    report: str | None = None,
    script: object = None,
    script_file: str | None = None,
    script_role: str | None = None,
    script_name: str | None = None,
    script_format: str | None = None,
    task_id: str | None = None,
    status: str | None = None,
    message: str | None = None,
    video_external: bool = False,
) -> Path:
    """Write one sanitized handoff into a persistent inbox without scanning.

    This is the implementation behind ``--no-sync``.  Local media and
    reports are copied next to the JSON record so a later watcher can resolve
    them without trusting an arbitrary absolute path.
    """
    manager_root = vpm_sync.resolve_root(root)
    manager_root.mkdir(parents=True, exist_ok=True)
    destination_dir = Path(task_dir).expanduser() if task_dir else manager_root / "inbox"
    try:
        if destination_dir.is_symlink():
            raise ReceiveError("接收目录不可用。")
    except OSError as exc:
        raise ReceiveError("接收目录不可用。") from exc
    destination_dir.mkdir(parents=True, exist_ok=True)
    if not destination_dir.is_dir() or destination_dir.is_symlink():
        raise ReceiveError("接收目录不可用。")

    with tempfile.TemporaryDirectory(prefix="vam-stage-") as temporary:
        stage = Path(temporary)
        task_path = _prepare_handoff(
            Path(source).expanduser(),
            stage,
            video_override=video,
            video_url_override=video_url,
            generator=generator,
            title=title,
            report_override=report,
            script_override=script,
            script_file_override=script_file,
            script_role_override=script_role,
            script_name_override=script_name,
            script_format_override=script_format,
            task_id_override=task_id,
            status_override=status,
            message_override=message,
            project_override=project,
            clip_override=clip,
            video_override_external=video_external,
            script_override_external=bool(script_file),
        )
        task = _read_json(task_path)
        safe_id = _safe_task_id(task.get("task_id"), task_path.stem)
        for field in ("video_file", "report_file", "script_file"):
            name = task.get(field)
            if not isinstance(name, str) or not name:
                continue
            source_file = stage / name
            if source_file.is_symlink() or not source_file.is_file():
                continue
            target_name = f"{safe_id}-{Path(name).name}"
            target = destination_dir / target_name
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ReceiveError("接收目录中的目标文件不可用。")
            try:
                shutil.copy2(source_file, target)
            except OSError as exc:
                raise ReceiveError("结果文件暂存失败。") from exc
            task[field] = target_name
        task_path_out = destination_dir / f"{safe_id}.json"
        if task_path_out.is_symlink() or (task_path_out.exists() and not task_path_out.is_file()):
            raise ReceiveError("接收目录中的目标文件不可用。")
        _write_json_atomic(task_path_out, task)
        return task_path_out


def _watch_inputs(directory: Path) -> list[Path]:
    """Return result envelopes plus unreferenced media files.

    A result directory may contain a JSON envelope and the copied video.  Do
    not enqueue that video twice when the envelope already points at it, but
    do keep a newly written standalone video visible when an older/stale JSON
    file is present.  This is important for generators that emit files first
    and metadata later (or never emit metadata at all).
    """
    task_files = _candidate_task_files(directory)
    videos = _candidate_videos(directory, recursive=False)
    if not task_files:
        return videos[:MAX_VIDEO_CANDIDATES]

    # Compare references without trusting or opening them.  The actual
    # receive path re-validates every file against approved roots; this set is
    # only a duplicate-suppression hint for the watcher.  Resolve through the
    # same approved-root adapter used by the real receiver.  In particular,
    # do not suppress a local bare file merely because an unresolved worker
    # path happens to have the same basename: that would strand generators
    # that emit a virtual path and a colocated file without an ``output_dir``.
    referenced: set[str] = set()
    for task_file in task_files:
        try:
            task = _read_json(task_file)
        except ReceiveError:
            continue
        resolved = _local_reference(task, task_file)
        if resolved is not None:
            try:
                referenced.add(str(resolved.resolve(strict=False)).casefold())
            except (OSError, RuntimeError):
                referenced.add(str(resolved).casefold())
        try:
            values = handoff_module.iter_video_references(task)
        except Exception:
            values = ()
        for value in values:
            if not isinstance(value, str) or re.match(r"^(?:https?|s3|data):", value.strip(), re.I):
                continue
            text = value.strip().split("?", 1)[0].split("#", 1)[0]
            try:
                raw = Path(text).expanduser()
                candidate = raw if raw.is_absolute() else directory / raw
                resolved = candidate.resolve(strict=False)
                # Only an existing, directly named file is a trustworthy
                # same-directory reference.  A missing virtual path is left
                # for the bounded bare-file fallback below.
                if resolved.is_file() and not resolved.is_symlink():
                    referenced.add(str(resolved).casefold())
            except (OSError, RuntimeError, ValueError):
                continue

    unreferenced: list[Path] = []
    for video in videos:
        try:
            resolved_key = str(video.resolve(strict=False)).casefold()
        except (OSError, RuntimeError):
            resolved_key = str(video).casefold()
        if resolved_key in referenced:
            continue
        unreferenced.append(video)
    return [*task_files, *unreferenced[:MAX_VIDEO_CANDIDATES]]


def discover_output_candidates(
    directory: str | os.PathLike[str],
    *,
    limit: int = MAX_VIDEO_CANDIDATES,
) -> list[Path]:
    """Return completed output envelopes plus standalone video files.

    The manager's background fallback must not turn every failed/provider task
    JSON into a visible pending record.  A JSON candidate therefore needs a
    terminal completion signal and a usable local/remote video reference.  A
    bare video file is itself a completed public output.
    """

    source_dir = Path(directory).expanduser().resolve(strict=False)
    if not source_dir.is_dir() or source_dir.is_symlink():
        return []
    try:
        bounded = max(1, min(int(limit), MAX_VIDEO_CANDIDATES))
    except (TypeError, ValueError):
        bounded = MAX_VIDEO_CANDIDATES
    result: list[Path] = []
    for candidate in _watch_inputs(source_dir):
        if len(result) >= bounded:
            break
        if candidate.suffix.lower() in VIDEO_EXTS:
            result.append(candidate)
            continue
        if candidate.suffix.lower() != ".json":
            continue
        try:
            task = _read_json(candidate)
        except ReceiveError:
            continue
        status = vpm_sync.task_status(task)
        terminal = status in vpm_sync.TERMINAL_STATUSES
        if not terminal:
            try:
                terminal = bool(handoff_module.generation_completion_confirmed(task))
            except Exception:
                terminal = False
        if not terminal:
            continue
        if _local_reference(task, candidate) is None and _remote_reference(task) is None:
            continue
        result.append(candidate)
    return result


def watch(
    directory: str | os.PathLike[str],
    *,
    interval: float = 2.0,
    once: bool = False,
    **receive_kwargs: Any,
) -> int:
    """Watch a result directory and receive each stable new result."""
    source_dir = Path(directory).expanduser().resolve(strict=False)
    if not source_dir.is_dir() or source_dir.is_symlink():
        raise ReceiveError("监听目录不可用。")
    try:
        delay = max(0.25, min(float(interval), 300.0))
    except (TypeError, ValueError):
        delay = 2.0
    # ``no_start`` is a CLI-only option.  Do not forward it to ``receive``
    # below (``receive`` deliberately exposes the clearer ``start_runtime``
    # API instead).  Copy the kwargs so callers do not observe mutation.
    receive_options = dict(receive_kwargs)
    no_start = bool(receive_options.pop("no_start", False))

    # Starting/probing before the first result gives a true one-step local
    # workflow; subsequent receives reuse that runtime.
    with tempfile.TemporaryDirectory(prefix="vam-receive-watch-"):
        empty = Path(tempfile.gettempdir()) / f"vam-empty-{os.getpid()}-{time.time_ns()}"
        empty.mkdir(parents=True, exist_ok=True)
        try:
            initial = vpm_sync.scan(
                vpm_sync.resolve_root(receive_options.get("root")),
                empty,
                start_runtime=not no_start,
                port=int(receive_options.get("port", DEFAULT_PORT)),
                bind=str(receive_options.get("bind", DEFAULT_BIND)),
                max_tasks=1,
            )
            print(json.dumps(_compact_scan(initial), ensure_ascii=False), flush=True)
        finally:
            try:
                empty.rmdir()
            except OSError:
                pass

    seen: dict[str, tuple[int, int]] = {}
    # Failed deliveries remain retryable; throttle repeated attempts for the
    # same file signature without marking them permanently seen.
    failed_at: dict[str, tuple[tuple[int, int], float]] = {}

    def receive_succeeded(result: object) -> bool:
        """Whether a candidate can be marked seen by this watcher.

        ``vpm_sync`` deliberately returns ``ok=true`` for a *pending* queue
        item and for a partial delivery (the record was safely persisted, but
        the media may still be cacheable later).  Treating that transport
        success as final made the watcher stop retrying exactly the handoffs
        that needed another pass.  Only a delivered/idempotent row with a
        public output is terminal here; pending or output-less partial rows
        stay retryable while their file signature is unchanged.
        """
        if not isinstance(result, dict) or result.get("ok") is not True:
            return False
        pending = result.get("pending_count")
        if pending is None:
            pending_value = result.get("pending")
            if isinstance(pending_value, list):
                pending = len(pending_value)
            else:
                try:
                    pending = int(pending_value or 0)
                except (TypeError, ValueError):
                    pending = 0
        try:
            if int(pending or 0) > 0:
                return False
        except (TypeError, ValueError):
            return False
        received = result.get("received")
        if not isinstance(received, list) or not received:
            return False
        for row in received:
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

    while True:
        candidates = _watch_inputs(source_dir)
        for candidate in candidates:
            try:
                stat = candidate.stat()
                signature = (int(stat.st_mtime_ns), int(stat.st_size))
            except OSError:
                continue
            key = str(candidate)
            if seen.get(key) == signature:
                continue
            prior_failure = failed_at.get(key)
            if (prior_failure and prior_failure[0] == signature
                    and time.monotonic() - prior_failure[1] < max(delay, 1.0)):
                continue
            # Avoid handing off a file while a generator is still writing it.
            time.sleep(0.15)
            try:
                check = candidate.stat()
                if (int(check.st_mtime_ns), int(check.st_size)) != signature:
                    continue
            except OSError:
                continue
            try:
                result = receive(candidate, start_runtime=False, **receive_options)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if receive_succeeded(result):
                    seen[key] = signature
                    failed_at.pop(key, None)
                else:
                    failed_at[key] = (signature, time.monotonic())
            except ReceiveError as exc:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), flush=True)
                failed_at[key] = (signature, time.monotonic())
        if once:
            return 0
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return 0


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("端口无效。") from exc
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError("端口无效。")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("receive", "notify", "watch"), default="receive")
    parser.add_argument("input", nargs="?", help="handoff JSON、视频文件或结果目录")
    parser.add_argument("--root", default=None, help="manager data root")
    parser.add_argument("--project", default=None, help="existing project slug")
    parser.add_argument("--clip", default=None, help="clip id")
    parser.add_argument("--port", type=_port, default=int(os.environ.get("VAM_PORT", DEFAULT_PORT)))
    parser.add_argument("--bind", default=os.environ.get("VAM_BIND", DEFAULT_BIND))
    parser.add_argument("--map", dest="mapping_file", default=None, help="task-to-project map JSON")
    parser.add_argument("--video", default=None, help="video path when input is a JSON/directory")
    parser.add_argument("--video-url", default=None, help="remote HTTPS/S3 video reference when input is a JSON/directory")
    parser.add_argument("--generator", default=None, help="public generator label")
    parser.add_argument("--title", default=None, help="public project title")
    parser.add_argument("--report", default=None, help="public report path or text")
    parser.add_argument(
        "--script", "--input-script", dest="script", default=None,
        help="public original input script/brief text",
    )
    parser.add_argument(
        "--script-file", "--input-script-file", dest="script_file", default=None,
        help="public original input script file",
    )
    parser.add_argument("--script-role", default=None,
                        choices=sorted(SCRIPT_ROLE_VALUES),
                        help="script, storyboard, subtitle, or analysis_report")
    parser.add_argument("--script-name", default=None, help="public script label")
    parser.add_argument("--script-format", default=None, help="public script format label")
    parser.add_argument("--task-id", default=None, help="stable public task identifier")
    parser.add_argument("--status", default=None, help="terminal result status")
    parser.add_argument("--message", default=None, help="short user-visible result message")
    parser.add_argument("--interval", type=float, default=2.0, help="watch polling interval in seconds")
    parser.add_argument("--once", action="store_true", help="watch one pass and exit")
    parser.add_argument("--no-start", action="store_true", help="do not start/probe the runtime")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.input:
        print(json.dumps({"ok": False, "error": "请提供结果文件或目录。"}, ensure_ascii=False))
        return 2
    kwargs = {
        "root": args.root,
        "project": args.project,
        "clip": args.clip,
        "port": args.port,
        "bind": str(args.bind or DEFAULT_BIND),
        "mapping_file": args.mapping_file,
        "video": args.video,
        "video_url": args.video_url,
        "generator": args.generator,
        "title": args.title,
        "report": args.report,
        "script": args.script,
        "script_file": args.script_file,
        "script_role": args.script_role,
        "script_name": args.script_name,
        "script_format": args.script_format,
        "task_id": args.task_id,
        "status": args.status,
        "message": args.message,
        # A path passed directly on this CLI is an explicit user choice. A
        # path embedded in a task JSON remains constrained to approved roots.
        "video_external": bool(args.video and not re.match(r"^(?:https?|s3)://", str(args.video).strip(), re.I)),
    }
    try:
        if args.command == "watch":
            return watch(args.input, interval=args.interval, once=args.once,
                         no_start=args.no_start, **kwargs)
        result = receive(args.input, start_runtime=not args.no_start, **kwargs)
    except ReceiveError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    except (OSError, RuntimeError, ValueError) as exc:
        # Never print raw provider/stack errors through this customer-facing
        # bridge.  The manager's private server log remains the diagnostic sink.
        _ = exc
        print(json.dumps({"ok": False, "error": "视频结果接收失败。"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
