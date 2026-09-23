#!/usr/bin/env python3
"""Receive a completed video-generator result into a video-asset-manager project.

This helper is intentionally a one-way handoff.  It never submits a generation
request or copies the source task JSON.  It accepts a local video file or a
provider-supplied remote video URL, caches the latter inside the project, and
stores only the resulting project-relative file plus public report text.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# The recorder is the single writer for project manifests and index.json.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from vpm_record import (  # noqa: E402
    ABSOLUTE_RE,
    P,
    PRIVATE_RE,
    SLUG_RE,
    derivatives,
    jload,
    jwrite,
    norm_rel,
    normalize_project,
    probe_media_duration,
    stable_id,
    sync_index,
    now,
)
try:
    from vpm_privacy import (  # type: ignore[import-not-found]
        contains_private_text,
        manager_child_environment,
        redact_recursive,
        redact_remote_reference,
        sanitize_public_text,
    )
except Exception:  # pragma: no cover - old copied bundle fallback
    def manager_child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
        source = dict(os.environ if base is None else base)
        return {
            key: value
            for key, value in source.items()
            if not re.search(
                r"(?:API[_-]?KEY|ACCESS[_-]?KEY|SECRET|TOKEN|PASSWORD|AUTH|"
                r"CREDENTIAL|PRIVATE[_-]?KEY|SIGNATURE)",
                str(key),
                re.I,
            )
        }

    def contains_private_text(value: object, *, allow_public_url: bool = False) -> bool:
        text = str(value or "")
        return bool(PRIVATE_RE.search(text) or ABSOLUTE_RE.search(text)
                    or (not allow_public_url and re.search(r"(?:https?|s3|file|data|ftp)://|(?<!\w)//[^\s]+", text, re.I)))

    def sanitize_public_text(value: object, *, limit: int = 4000,
                             fallback: str | None = None,
                             allow_public_url: bool = False) -> str | None:
        text = str(value or "").replace("\x00", "").strip()
        return fallback if not text or len(text) > limit or contains_private_text(text, allow_public_url=allow_public_url) else text

    def redact_recursive(value: object, *, depth: int = 0) -> object:
        if depth > 12:
            return None
        if isinstance(value, dict):
            result: dict[str, object] = {}
            for key, child in value.items():
                if contains_private_text(key):
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

    def redact_remote_reference(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        return re.sub(r"[?#].*$", "", value.strip()) or None


VIDEO_EXTS = {
    ".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi", ".mpeg", ".mpg",
    ".ts", ".ogv", ".3gp", ".flv",
}
# Audio extracted from a delivered generated video is written as AAC in an
# M4A container.  M4A is broadly playable in the management window while
# avoiding a second video codec/container dependency.  The source video is
# never modified; extraction is an append-only derivative operation.
AUDIO_EXT = ".m4a"
MAX_AUDIO_BYTES = 512 * 1024 * 1024
# ffmpeg's smallest valid container can be only a few dozen bytes (for
# example, a very short generated tone), so reject only an empty output here.
MIN_AUDIO_BYTES = 1
# Public script/input files are deliberately separate from generated media.
# These extensions cover the ordinary script, storyboard, subtitle, and
# structured-brief formats while keeping binary/provider payloads out of the
# project boundary.
SCRIPT_EXTS = {
    ".md", ".markdown", ".txt", ".text", ".json", ".jsonl", ".yaml",
    ".yml", ".srt", ".vtt", ".csv",
}
SCRIPT_INLINE_KEYS = (
    # ``input_script`` is the preferred generator-neutral name.  Explicit
    # ``public_*`` aliases are accepted for adapters that want to make the
    # visibility contract obvious.  Ambiguous prompt/user fields are never
    # accepted because they may contain a private transformed prompt.
    "input_script", "public_script", "public_input",
)
SCRIPT_FILE_KEYS = (
    "input_script_file", "public_script_file", "script_file",
)
SCRIPT_ROLE_KEYS = ("input_script_role", "public_script_role")
SCRIPT_NAME_KEYS = ("input_script_name", "public_script_name")
SCRIPT_FORMAT_KEYS = ("input_script_format", "public_script_format")
SCRIPT_PRIVATE_KEY_RE = re.compile(
    r"(?:^|_)(?:prompt|private|internal|diagnostic|provider|model|route|"
    r"credential|secret|token|api[_-]?key|signature)(?:_|$)", re.I,
)
MAX_SCRIPT_BYTES = 2 * 1024 * 1024
LOCAL_VIDEO_ENV_KEYS = (
    "VIDEO_GENERATOR_OUTPUT_DIR",
    "VIDEO_GENERATOR_OUTPUT_ROOT",
    "VAM_OUTPUT_DIR",
    "CAPAFY_OUTPUT_DIR",
    "CAPAFY_WORKSPACE",
)
# Capafy can report a path from its Linux worker even when the task JSON is
# being consumed by a Windows manager.  Treat this as a virtual local path;
# it must be mapped to an explicitly approved local root before opening it.
LOCAL_VIRTUAL_PREFIX_RE = re.compile(
    r"^[/\\]+home[/\\]+user[/\\]+(?P<bucket>outputs|agent-outputs)"
    r"(?:[/\\]+(?P<relative>.+))?$",
    re.IGNORECASE,
)
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
CLIP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
MAX_TASK_BYTES = 8 * 1024 * 1024
MAX_REMOTE_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
REMOTE_TIMEOUT_SECONDS = 300
REMOTE_VIDEO_KEYS = (
    "video_url", "videoUrl", "download_url", "downloadUrl", "file_url", "fileUrl",
    "object_url", "objectUrl", "s3_url", "s3Url", "video_uri", "videoUri",
    "resource_url", "resourceUrl", "download_link", "downloadLink",
    "object_key", "objectKey", "file_key", "fileKey", "resource_key", "resourceKey",
    "video_file", "videoFile", "video_path", "videoPath", "local_video",
)
REMOTE_VIDEO_NESTED_KEYS = (
    "objectKey", "object_key", "fileKey", "file_key", "resourceKey", "resource_key",
    "videoKey", "video_key", "uri", "url", "href", "path", "location", "key",
)
# Common result envelopes emitted by video-generation runners.  These names
# are deliberately allow-listed: traversing arbitrary task JSON would risk
# treating a prompt/report URL as a media location.
VIDEO_RESULT_CONTAINER_KEYS = {
    "task", "result", "data", "response", "payload", "output", "outputs",
    "content", "delivery", "artifact", "artifacts", "media",
}

# A task may have rendered successfully while the final object-store link (or
# the local cache) failed afterwards.  Keep that distinction in the manager:
# it is a recoverable/incomplete handoff, not a second generation failure.
DELIVERY_FAILURE_STATUSES = {
    "signing_failed",
    "no_watermark_link_failed",
    "video_link_missing",
    "video_link_failed",
    "delivery_failed",
    "download_failed",
    "remote_cache_failed",
}
DELIVERY_FAILURE_STAGES = {
    "video_link_signing",
    "video_link_no_watermark",
    "video_link_missing",
    "video_delivery",
    "video_download",
    "delivery",
}
GENERIC_FAILURE_REASONS = {
    "the video service rejected the request",
    "the video service returned an unreadable response",
    "the video could not be completed because the generation worker stopped unexpectedly",
    "the video generation result could not be confirmed",
}
FAILURE_STAGE_MESSAGES = {
    "generation": "视频生成未完成。",
    "video_generation": "视频生成未完成。",
    "generation_result_missing": "视频结果无法确认。",
    "status_check": "视频状态暂时无法确认。",
    "timeout": "视频结果未在规定时间内返回。",
}
DELIVERY_UNAVAILABLE_MESSAGE = "视频已生成，但交付链接不可用，暂时没有本地视频文件。"
DELIVERY_INCOMPLETE_MESSAGE = "视频已生成，但交付尚未完成，暂时没有本地视频文件。"


def path_within(base: Path, candidate: Path) -> bool:
    """Resolve both paths and require the candidate to remain under base."""
    try:
        base_real = os.path.normcase(str(base.resolve(strict=False)))
        candidate_real = os.path.normcase(str(candidate.resolve(strict=False)))
        return os.path.commonpath((base_real, candidate_real)) == base_real
    except (OSError, RuntimeError, ValueError):
        return False


def _remote_candidates(value: Any) -> list[str]:
    """Return safe transport candidates without persisting the original URL."""

    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text or len(text) > 16_384:
        return []
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return []
    if text.startswith("//"):
        text = "https:" + text
    try:
        parsed = urllib.parse.urlparse(text)
    except ValueError:
        return []
    if parsed.username is not None or parsed.password is not None:
        return []
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"} and parsed.netloc:
        path = parsed.path or ""
        for _ in range(3):
            decoded = urllib.parse.unquote(path)
            if decoded == path:
                break
            path = decoded
        path = path.replace("\\", "/")
        if "\x00" in path or any(part == ".." for part in path.split("/") if part not in ("", ".")):
            return []
        return [text]
    if scheme == "s3" and parsed.netloc and parsed.path.strip("/"):
        # Treat an S3 reference as an object identifier, not a filesystem
        # path.  Reject traversal/NUL segments before constructing either
        # public S3 spelling; this also keeps a malformed key from being
        # normalized differently by the two fallback hosts.
        raw_key = urllib.parse.unquote(parsed.path.lstrip("/")).replace("\\", "/")
        parts = [part for part in raw_key.split("/") if part not in ("", ".")]
        if not parts or any(part == ".." or "\x00" in part for part in parts):
            return []
        key = urllib.parse.quote("/".join(parts), safe="/~-._")
        bucket = parsed.netloc.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{1,62}", bucket):
            return []
        # Virtual-hosted style is the common public/S3-compatible form.  The
        # path-style fallback handles buckets whose DNS name is not available.
        query = f"?{parsed.query}" if parsed.query else ""
        return [
            f"https://{bucket}.s3.amazonaws.com/{key}{query}",
            f"https://s3.amazonaws.com/{bucket}/{key}{query}",
        ]
    return []


def _host_is_safe(host: str) -> bool:
    """Reject local/private destinations before following a remote media URL."""

    host = (host or "").strip().strip("[]").rstrip(".").lower()
    if not host:
        return False
    # A test/development caller may explicitly opt into a loopback fixture;
    # production handoffs remain public-network-only by default.
    allow_local = os.environ.get("VAM_ALLOW_LOCAL_REMOTE") == "1"
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return allow_local
    try:
        address = ipaddress.ip_address(host)
        return allow_local or not (
            address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_reserved or address.is_unspecified
        )
    except ValueError:
        pass
    try:
        resolved = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        # A hostname whose destination cannot be resolved is not safe to fetch;
        # the next manager scan can try again if DNS recovers.
        return False
    for item in resolved:
        try:
            address = ipaddress.ip_address(item[4][0])
        except (ValueError, IndexError):
            continue
        if address.is_private or address.is_loopback or address.is_link_local \
                or address.is_multicast or address.is_reserved or address.is_unspecified:
            return allow_local
    return True


def _safe_remote_url(value: str) -> bool:
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in str(value or "")):
        return False
    try:
        parsed = urllib.parse.urlparse(value)
        hostname = parsed.hostname or ""
    except ValueError:
        return False
    try:
        return parsed.scheme.lower() in {"http", "https"} and not parsed.username \
            and not parsed.password and bool(parsed.netloc) and _host_is_safe(hostname)
    except ValueError:
        return False


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only when the next host remains an allowed destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if not _safe_remote_url(newurl):
            raise ValueError("remote redirect destination is not allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _video_suffix(url: str, content_type: str = "", disposition: str = "") -> str:
    candidates: list[str] = []
    try:
        candidates.append(Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).suffix.lower())
    except (TypeError, ValueError):
        pass
    match = re.search(r"filename\*?=(?:UTF-8''|\")?([^;\"]+)", disposition or "", re.I)
    if match:
        candidates.append(Path(urllib.parse.unquote(match.group(1).strip())).suffix.lower())
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    candidates.append({
        "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
        "video/x-matroska": ".mkv", "video/x-msvideo": ".avi", "video/mpeg": ".mpeg",
        "video/ogg": ".ogv", "video/3gpp": ".3gp", "video/mp2t": ".ts",
    }.get(mime, ""))
    for suffix in candidates:
        if suffix in VIDEO_EXTS:
            return suffix
    return ".mp4"


def _looks_like_video(path: Path, *, content_type: str = "", suffix: str = "") -> bool:
    try:
        if path.stat().st_size < 1024:
            return False
        with path.open("rb") as handle:
            header = handle.read(512)
    except OSError:
        return False
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime.startswith(("text/", "application/json", "application/xml")):
        return False
    if b"ftyp" in header[:256] or header.startswith(b"\x1a\x45\xdf\xa3"):
        return True
    if header[:4] == b"RIFF" and b"AVI " in header[:32]:
        return True
    if mime.startswith("video/"):
        return True
    if header.lstrip().startswith((b"<", b"{", b"[")):
        return False
    return suffix.lower() in VIDEO_EXTS


def _iter_video_references(value: Any, *, depth: int = 0,
                           video_context: bool = False) -> Iterable[str]:
    """Yield media-like strings from a bounded, known result envelope.

    A routed runner commonly returns ``{"task": {"content": {
    "downloaded_files": [...]}}}``.  The old adapter only inspected the
    top-level object, which made a perfectly good local/remote result look
    missing.  Keep traversal narrow and deterministic so prompts, reports,
    and arbitrary user text are never interpreted as media.
    """
    if depth > 8:
        return
    if isinstance(value, str):
        yield value.strip()
        return
    if isinstance(value, list):
        for child in value[:100]:
            yield from _iter_video_references(
                child, depth=depth + 1, video_context=video_context
            )
        return
    if not isinstance(value, dict):
        return

    # Explicit media fields and names containing a media token have priority.
    # Their children are now in a video context, so generic URL/path aliases
    # (``url``, ``path``, ``key``) are safe to inspect one level below.
    handled: set[str] = set()
    for raw_key, child in value.items():
        key = str(raw_key)
        normalized = key.lower().replace("-", "_")
        if key in REMOTE_VIDEO_KEYS or any(
            token in normalized
            for token in ("video", "download", "file", "object", "media")
        ):
            handled.add(key)
            yield from _iter_video_references(
                child, depth=depth + 1, video_context=True
            )

    # Walk only well-known result envelopes (including ``task`` and
    # ``content``).  The envelope itself establishes a media context, so a
    # common ``{"result": {"url": "..."}}`` response is accepted while
    # unrelated top-level/report URLs remain ignored.
    for raw_key, child in value.items():
        key = str(raw_key)
        normalized = key.lower().replace("-", "_")
        if key in handled or normalized not in VIDEO_RESULT_CONTAINER_KEYS:
            continue
        yield from _iter_video_references(
            child, depth=depth + 1, video_context=True
        )

    if video_context:
        for key in REMOTE_VIDEO_NESTED_KEYS:
            if key in value:
                yield from _iter_video_references(
                    value.get(key), depth=depth + 1, video_context=True
                )


def iter_video_references(value: Any) -> Iterable[str]:
    """Public bounded iterator shared by producer-facing receive helpers."""
    yield from _iter_video_references(value)


def remote_video_reference(task: dict[str, Any]) -> str | None:
    """Read a provider URL from a task without returning arbitrary text."""

    for candidate in iter_video_references(task):
        if _remote_candidates(candidate):
            return candidate.strip()
    return None


def cache_remote_video(reference: str, stage_dir: Path, task_id: str) -> tuple[Path | None, str | None]:
    """Download a provider URL into a private staging directory.

    The URL is used only in memory.  The caller copies the bytes into the
    append-only project clip path and never writes the URL to a manifest, log,
    task summary, or ZIP archive.
    """

    candidates = [candidate for candidate in _remote_candidates(reference) if _safe_remote_url(candidate)]
    if not candidates:
        return None, "远程视频地址不可用。"
    stage_dir.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    last_error = "远程视频暂时无法缓存。"
    for index, url in enumerate(candidates):
        destination: Path | None = None
        temporary: Path | None = None
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "video-asset-manager/2"})
            with opener.open(request, timeout=REMOTE_TIMEOUT_SECONDS) as response:
                content_type = response.headers.get("Content-Type", "")
                disposition = response.headers.get("Content-Disposition", "")
                suffix = _video_suffix(url, content_type, disposition)
                destination = stage_dir / f"remote-{task_id}-{index}{suffix}"
                fd, temp_name = tempfile.mkstemp(prefix=".remote.", suffix=suffix, dir=str(stage_dir))
                os.close(fd)
                temporary = Path(temp_name)
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_REMOTE_VIDEO_BYTES:
                    raise ValueError("remote video is too large")
                total = 0
                with temporary.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_REMOTE_VIDEO_BYTES:
                            raise ValueError("remote video is too large")
                        handle.write(chunk)
                if not _looks_like_video(temporary, content_type=content_type, suffix=suffix):
                    raise ValueError("remote response is not a video")
                os.replace(str(temporary), str(destination))
                return destination, None
        except Exception as exc:  # noqa: BLE001 - keep public reason generic
            last_error = "远程视频暂时无法缓存。"
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass
            # Try the path-style S3 candidate after a virtual-hosted failure;
            # ordinary HTTPS URLs have only one candidate and exit here.
            if index + 1 >= len(candidates):
                _ = exc
    return None, last_error


def approved_task_roots(task: dict[str, Any], task_file: Path | None) -> list[Path]:
    """Return local roots from which a task may provide public outputs.

    Generator task files normally live in the system task directory and point at
    the selected output directory.  Restricting reads to those roots prevents
    a tampered task JSON from making a handoff copy an arbitrary local file.
    """
    roots: list[Path] = []
    if task_file is not None:
        roots.append(task_file.parent)
    for output_key in ("output_dir", "output_root", "artifact_dir", "media_dir"):
        output_dir = task.get(output_key)
        if isinstance(output_dir, str) and output_dir.strip() and not re.match(r"^(?:https?|s3|data):", output_dir.strip(), re.I):
            try:
                roots.append(Path(output_dir).expanduser())
            except (TypeError, ValueError, OSError):
                pass
    # Conventional generator directories supplement an explicit output_dir.
    roots.extend((Path(tempfile.gettempdir()) / "video-generator-handoffs",
                  Path(tempfile.gettempdir()) / "video-generator-outputs"))
    # Capafy may hand back a workspace-relative path such as
    # a root-relative media path while the task itself is running in a mounted
    # workspace.  Include the process workspace and the configured Capafy
    # workspace as bounded local intake roots so that path can be resolved
    # after the result is copied back to this machine.
    for variable in LOCAL_VIDEO_ENV_KEYS:
        value = os.environ.get(variable)
        if value and value.strip():
            try:
                configured = Path(value.strip()).expanduser()
                roots.append(configured)
                if variable == "CAPAFY_WORKSPACE":
                    # Capafy exposes either the workspace itself or one of
                    # these bounded handoff folders depending on the host.
                    roots.extend((
                        configured / "outputs",
                        configured / "agent-outputs",
                        configured / ".capafy" / "video-asset-manager" / "inbox",
                        configured / ".capafy" / "video-generator-outputs",
                    ))
            except (TypeError, ValueError, OSError):
                pass
    roots.extend((Path.cwd(), Path.cwd() / "outputs", Path.home() / "workspace"))
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = root.expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def _virtual_local_candidates(text: str, roots: list[Path]) -> list[Path]:
    """Map a Capafy worker path to bounded local output roots.

    The path is only a hint.  We deliberately do not search the filesystem by
    basename or walk a whole workspace: each candidate is a direct child of a
    root already approved by the task/output configuration or environment.
    """
    normalized = text.replace("\\", "/")
    for _ in range(3):
        decoded = urllib.parse.unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    match = LOCAL_VIRTUAL_PREFIX_RE.fullmatch(normalized)
    if not match:
        return []
    bucket = str(match.group("bucket") or "").lower()
    relative_text = str(match.group("relative") or "")
    for _ in range(3):
        decoded = urllib.parse.unquote(relative_text)
        if decoded == relative_text:
            break
        relative_text = decoded
    relative_text = relative_text.replace("\\", "/")
    parts = [part for part in relative_text.split("/") if part not in ("", ".")]
    if not parts or any(
        part == ".." or "\x00" in part or ":" in part
        for part in parts
    ):
        return []
    relative = Path(*parts)
    # A configured root may be the workspace itself, its outputs directory,
    # or an inbox.  Try the exact virtual bucket plus the sibling bucket to
    # cover Capafy's post-run agent-outputs copy without broadening the root.
    buckets = [bucket, "agent-outputs" if bucket == "outputs" else "outputs"]
    candidates: list[Path] = []
    for root in roots:
        for candidate_root in (root, *(root / name for name in buckets)):
            candidate = candidate_root / relative
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def approved_local_file(value: Any, task: dict[str, Any], task_file: Path | None,
                        extensions: set[str] | None = None,
                        *, allow_video_signature: bool = False) -> Path | None:
    """Resolve a task-provided file only when it stays in an approved root."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) > 16_384 or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        return None
    if re.match(r"^(?:https?|s3|data):", text, re.I):
        return None
    if text.lower().startswith("file://"):
        try:
            parsed = urllib.parse.urlparse(text)
        except ValueError:
            return None
        if parsed.netloc and parsed.netloc.lower() not in {"localhost", "127.0.0.1"}:
            return None
        text = urllib.parse.unquote(parsed.path or "")
        if re.match(r"^/[A-Za-z]:[\\/]", text):
            text = text[1:]
        if not text:
            return None
    try:
        raw = Path(text).expanduser()
    except (TypeError, ValueError, OSError):
        return None
    roots = approved_task_roots(task, task_file)
    candidates: list[Path] = []
    # A Capafy path is a worker-runtime reference, not a local Windows drive
    # path.  Resolve its relative tail against the bounded roots first.
    candidates.extend(_virtual_local_candidates(text, roots))
    if raw.is_absolute():
        candidates.append(raw)
        # Windows Capafy paths can begin with a single slash but still mean a
        # file relative to the mounted workspace rather than the drive root.
        if text.startswith(("\\", "/")):
            relative = text.lstrip("\\/")
            candidates.extend(root / relative for root in roots)
    else:
        if task_file is not None:
            candidates.append(task_file.parent / raw)
        candidates.extend(root / raw for root in roots)
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_file():
            continue
        suffix = resolved.suffix.lower()
        if extensions and suffix not in extensions:
            if not (allow_video_signature and _looks_like_video(resolved, suffix=suffix)):
                continue
        # The helper is also used for small Markdown/JSON reports; apply the
        # media size guard only when the resolved suffix is a video type.
        if suffix in VIDEO_EXTS or allow_video_signature:
            try:
                size = resolved.stat().st_size
            except OSError:
                continue
            if size < 1024 or size > MAX_REMOTE_VIDEO_BYTES:
                continue
        if any(path_within(root, resolved) for root in roots):
            return resolved
    return None


def fail(message: str, code: int = 1) -> None:
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
    raise SystemExit(code)


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp.", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def extract_audio_track(
    project: "P",
    source_rel: str,
    clip_id: str,
    video_digest: str | None,
) -> tuple[str | None, str | None]:
    """Extract the first audio stream from a delivered video, fail-open.

    The manager receives videos from arbitrary generators, so it cannot assume
    that a separate audio artifact or a particular container is returned.  A
    small AAC/M4A derivative gives the management page a playable audio asset
    whenever the source actually contains an audio stream.  Every failure is
    converted to a short, non-sensitive reason and returned to the caller;
    callers must keep the video delivery successful even when ffmpeg is absent,
    the source has no audio, or transcoding fails.

    ``source_rel`` and the deterministic destination are validated against the
    project root before invoking ffmpeg.  The output is written through a
    temporary sibling and atomically renamed so an interrupted extraction can
    never leave a partially-written public asset.  Repeating the same handoff
    reuses an existing regular output and therefore remains idempotent.
    """

    if not video_digest:
        return None, "视频摘要不可用，未提取音频。"
    source_rel = norm_rel(source_rel) or ""
    if not source_rel or not project.rel_ok(source_rel):
        return None, "视频路径不可用，未提取音频。"

    source = Path(project.dir) / source_rel
    try:
        if source.is_symlink() or not source.is_file():
            return None, "视频文件不可用，未提取音频。"
        if source.stat().st_size < 1:
            return None, "视频文件不可用，未提取音频。"
    except OSError:
        return None, "视频文件不可用，未提取音频。"

    safe_clip = re.sub(r"[^A-Za-z0-9._-]", "_", str(clip_id or "clip"))[:80] or "clip"
    # The digest is the source identity: a new video version receives a new
    # audio path, while rescanning the same result never appends a duplicate.
    audio_rel = f"assets/generated/audio-{safe_clip}-{video_digest[:16]}{AUDIO_EXT}"
    if not project.rel_ok(audio_rel):
        return None, "音频路径不可用，未提取音频。"
    destination = Path(project.dir) / audio_rel
    try:
        if destination.is_symlink():
            return None, "音频路径不可用，未提取音频。"
        if destination.is_file():
            size = destination.stat().st_size
            if MIN_AUDIO_BYTES <= size <= MAX_AUDIO_BYTES:
                return audio_rel, None
    except OSError:
        return None, "音频路径不可用，未提取音频。"

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None, "ffmpeg 不可用，未提取音频。"

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=".audio-", suffix=AUDIO_EXT, dir=str(destination.parent)
        )
        os.close(fd)
        temporary = Path(temp_name)
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-map_metadata",
            "-1",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=180,
            check=False,
            env=manager_child_environment(),
        )
        if result.returncode != 0:
            return None, "视频没有可提取的音频轨道。"
        if not temporary.is_file():
            return None, "音频轨道未生成。"
        size = temporary.stat().st_size
        if size < MIN_AUDIO_BYTES or size > MAX_AUDIO_BYTES:
            return None, "音频轨道未生成。"
        # Refuse to overwrite a newly-created symlink; an existing regular
        # file is safe to replace because the path is digest-derived.
        if destination.is_symlink():
            return None, "音频路径不可用，未提取音频。"
        os.replace(str(temporary), str(destination))
        temporary = None
        return audio_rel, None
    except subprocess.TimeoutExpired:
        return None, "音频提取超时。"
    except (OSError, ValueError):
        return None, "音频轨道提取失败。"
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def register_extracted_audio(
    document: dict[str, Any],
    project: "P",
    *,
    audio_rel: str,
    clip_id: str,
    source_rel: str,
    video_digest: str,
) -> tuple[dict[str, Any] | None, bool]:
    """Register an extracted track as an AI-generated media asset.

    Audio extraction is a derivative of the generated video, but the customer
    needs it discoverable through the page's ``AI 生成`` filter.  Therefore the
    public record intentionally uses ``origin: generated`` and carries an
    explicit ``ai-generated`` tag plus a generic lineage sidecar.  No provider,
    prompt, URL, or absolute path is persisted.  The stable ID is based on the
    clip and source video digest, so a repeated scan updates the same record.
    """

    audio_rel = norm_rel(audio_rel) or ""
    source_rel = norm_rel(source_rel) or ""
    if not audio_rel or not source_rel or not project.rel_ok(audio_rel) or not project.rel_ok(source_rel):
        return None, False
    audio_path = Path(project.dir) / audio_rel
    try:
        if audio_path.is_symlink() or not audio_path.is_file():
            return None, False
        size = audio_path.stat().st_size
        if size < MIN_AUDIO_BYTES or size > MAX_AUDIO_BYTES:
            return None, False
        audio_digest = digest_file(audio_path)
    except (OSError, ValueError):
        return None, False

    asset_id = stable_id("as", "audio", clip_id, video_digest)
    sidecar_rel = audio_rel + ".json"
    sidecar_path = Path(project.dir) / sidecar_rel
    if not project.rel_ok(sidecar_rel) or sidecar_path.is_symlink():
        sidecar_rel = ""
        sidecar_path = None
    existing: dict[str, Any] | None = None
    media = document.setdefault("asset", {}).setdefault("media", [])
    for item in media:
        if not isinstance(item, dict):
            continue
        if item.get("id") == asset_id:
            existing = item
            break
        # A pre-existing record from an older manager may have the same file
        # but a different generated ID.  Reuse it rather than creating a card
        # duplicate when upgrading an installation.
        if norm_rel(item.get("file")) == audio_rel and item.get("kind") == "audio":
            existing = item
            asset_id = str(item.get("id") or asset_id)
            break

    created = (existing.get("created") if isinstance(existing, dict) else None) or now()
    canonical_entry: dict[str, Any] = {
        "id": asset_id,
        "kind": "audio",
        "file": audio_rel,
        "name": f"AI generated audio · {clip_id}",
        "origin": "generated",
        "group": "ai-generated",
        "tags": ["ai-generated", "extracted-audio"],
        "poster": None,
        "used_by": [clip_id],
        "status": "active",
        "hash": "sha256:" + audio_digest,
        "created": created,
        "gen": {
            "prompt_summary": "Audio track extracted from an AI-generated video.",
            "model_label": "audio",
            "params": {"operation": "extract-audio", "source": "video"},
            "parent": clip_id,
            "source_video": source_rel,
            "source_sha256": "sha256:" + video_digest,
            **({"sidecar": sidecar_rel} if sidecar_rel else {}),
        },
    }

    if existing is not None:
        # Preserve any unrelated customer labels/fields while enforcing the
        # generated-audio contract and refreshing clip lineage.
        entry = dict(existing)
        entry.update(canonical_entry)
        existing_tags = existing.get("tags") if isinstance(existing.get("tags"), list) else []
        entry["tags"] = [*existing_tags]
        for tag in ("ai-generated", "extracted-audio"):
            if tag not in entry["tags"]:
                entry["tags"].append(tag)
        existing_used = existing.get("used_by") if isinstance(existing.get("used_by"), list) else []
        entry["used_by"] = [*existing_used]
        if clip_id not in entry["used_by"]:
            entry["used_by"].append(clip_id)
    else:
        entry = canonical_entry
    changed = existing != entry

    media[:] = [item for item in media if not (isinstance(item, dict) and item.get("id") == asset_id)]
    media.append(entry)

    # A public, provider-neutral sidecar makes the lineage explicit in ZIP
    # exports.  Sidecar failure is deliberately fail-open: the audio record is
    # still useful and remains visible in the manager.
    if sidecar_path is not None:
        sidecar = {
            "prompt": "Audio track extracted from an AI-generated video.",
            "model_label": "audio",
            "params": {"operation": "extract-audio", "source": "video"},
            "parent": clip_id,
            "created": created,
        }
        try:
            current = jload(sidecar_path)
            if current != sidecar:
                jwrite(sidecar_path, sidecar)
                changed = True
        except (OSError, TypeError, ValueError):
            pass
    return entry, changed


def safe_identifier(value: Any, prefix: str, pattern: re.Pattern[str]) -> str:
    raw = str(value or "").strip()
    if pattern.fullmatch(raw):
        return raw
    return stable_id(prefix, raw or "missing")


def clean_public(value: Any, *, limit: int = 4000, fallback: str | None = None) -> str | None:
    """Return a short public string, rejecting obvious private/internal data."""
    return sanitize_public_text(value, limit=limit, fallback=fallback)


def task_value(task: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = task.get(key)
        if value is not None:
            return value
    return None


def generator_name(task: dict[str, Any]) -> str:
    """Return a safe public generator label for handoff bookkeeping."""
    value = task_value(task, "generator", "generator_name", "source", "producer")
    text = clean_public(value, limit=48, fallback=None) or "video-generator"
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")[:40]
    return text or "video-generator"


def _looks_like_local_path_reference(value: Any) -> bool:
    """Classify a value as a local-path hint without treating object keys as files."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    normalized = text.replace("\\", "/")
    for _ in range(3):
        decoded = urllib.parse.unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    if LOCAL_VIRTUAL_PREFIX_RE.fullmatch(normalized):
        return True
    if normalized.lower().startswith("file://"):
        return True
    if re.match(r"^[A-Za-z]:/", normalized) or normalized.startswith("/"):
        return True
    lower = normalized.lower().split("?", 1)[0].split("#", 1)[0]
    basename = lower.rsplit("/", 1)[-1]
    return (
        Path(basename).suffix in VIDEO_EXTS
        or lower.startswith(("outputs/", "output/", "agent-outputs/", "video-generator-outputs/"))
    )


def read_public_report(task: dict[str, Any], task_file: Path | None = None) -> tuple[str | None, str | None]:
    """Read report text or a local report file without copying task payloads."""
    value = task_value(task, "report", "report_file", "analysis_report")
    if not isinstance(value, str) or not value.strip():
        return None, None
    candidate = approved_local_file(value, task, task_file,
                                   {".md", ".markdown", ".txt", ".json", ".jsonl"})
    if candidate is not None:
        try:
            raw = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None, "分析报告无法读取。"
    else:
        raw = value
    cleaned = clean_public(raw, limit=2_000_000)
    if cleaned is None:
        return None, "分析报告未返回。"
    return cleaned, None


def _script_role(task: dict[str, Any]) -> str:
    """Return a bounded public role for a supplied input document."""
    value = task_value(task, *SCRIPT_ROLE_KEYS)
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "shot_list": "storyboard",
        "shotlist": "storyboard",
        "captions": "subtitle",
        "subtitles": "subtitle",
        "report": "analysis_report",
        "analysis": "analysis_report",
    }
    text = aliases.get(text, text)
    return text if text in {"script", "storyboard", "subtitle", "analysis_report"} else "script"


def _script_meta_text(task: dict[str, Any], keys: tuple[str, ...], limit: int,
                      fallback: str | None = None) -> str | None:
    value = task_value(task, *keys)
    return clean_public(value, limit=limit, fallback=fallback)


def _public_script_structure(value: Any, *, depth: int = 0) -> Any:
    """Redact private keys from structured public script input.

    A generator may provide a shot list as JSON rather than Markdown.  Keep
    useful narrative fields while dropping prompt/routing/diagnostic payloads
    even when they are nested below an otherwise public ``script`` object.
    """
    if depth > 12:
        return None
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if SCRIPT_PRIVATE_KEY_RE.search(key) or contains_private_text(key):
                continue
            clean = _public_script_structure(child, depth=depth + 1)
            if clean is not None:
                output[key] = clean
        return output
    if isinstance(value, list):
        result: list[Any] = []
        for child in value[:500]:
            clean = _public_script_structure(child, depth=depth + 1)
            if clean is not None:
                result.append(clean)
        return result
    if isinstance(value, str):
        # Structured values are serialized later; filter URL/path/credential
        # strings now so they cannot reappear through JSON encoding.
        return sanitize_public_text(value, limit=8_000, fallback=None)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return None


def sanitize_script_text(value: Any, suffix: str = "") -> str | None:
    """Normalize a public script/brief to bounded, customer-visible text.

    Markdown/plain text is filtered per line so one diagnostic line does not
    erase the customer's complete script.  Structured input is recursively
    redacted before serialization.  The returned value never contains an
    absolute path, transport URL, credential, or private routing field.
    """
    if isinstance(value, (dict, list)):
        clean_value = _public_script_structure(value)
        if clean_value is None:
            return None
        try:
            raw_text = json.dumps(clean_value, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return None
        suffix = ".json"
    elif isinstance(value, str):
        raw_text = value
    else:
        return None
    if len(raw_text.encode("utf-8", "ignore")) > MAX_SCRIPT_BYTES:
        return None
    suffix = (suffix or "").lower()
    if suffix in {".json", ".jsonl"} and isinstance(value, str):
        # Preserve a structured script's shape when it is supplied as a file;
        # malformed JSON falls back to safe line filtering below.
        try:
            parsed = json.loads(raw_text) if suffix == ".json" else None
        except (UnicodeError, json.JSONDecodeError):
            parsed = None
        if parsed is not None:
            clean_value = _public_script_structure(parsed)
            try:
                raw_text = json.dumps(clean_value, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                return None
        elif suffix == ".jsonl":
            lines: list[str] = []
            for line in raw_text.splitlines():
                try:
                    parsed_line = json.loads(line)
                except (UnicodeError, json.JSONDecodeError):
                    clean_line = sanitize_public_text(line, limit=8_000, fallback=None)
                else:
                    clean_value = _public_script_structure(parsed_line)
                    try:
                        clean_line = json.dumps(clean_value, ensure_ascii=False, separators=(",", ":")) 
                    except (TypeError, ValueError):
                        clean_line = None
                if clean_line:
                    lines.append(clean_line)
            raw_text = "\n".join(lines)
            return raw_text.strip() or None
    # Preserve paragraph/shot separation from the customer's original input
    # while filtering unsafe non-empty lines.  Collapsing every blank line
    # loses meaningful script formatting and makes a re-opened script differ
    # from what the customer supplied.
    lines: list[str] = []
    for line in raw_text.splitlines():
        if not line.strip():
            if lines and lines[-1] != "":
                lines.append("")
            continue
        clean_line = sanitize_public_text(line, limit=8_000, fallback=None)
        if clean_line:
            lines.append(clean_line)
    while lines and lines[-1] == "":
        lines.pop()
    cleaned = "\n".join(lines).strip()
    if not cleaned or len(cleaned.encode("utf-8", "ignore")) > MAX_SCRIPT_BYTES:
        return None
    return cleaned


def public_script_info(value: Any, *, name: Any = None, role: Any = None,
                       fmt: Any = None, suffix: str = "") -> dict[str, str] | None:
    """Build a sanitized script descriptor from inline public input."""
    text = sanitize_script_text(value, suffix)
    if not text:
        return None
    role_text = str(role or "").strip().lower().replace("-", "_")
    role_text = {"shot_list": "storyboard", "shotlist": "storyboard",
                 "captions": "subtitle", "subtitles": "subtitle",
                 "report": "analysis_report", "analysis": "analysis_report"}.get(role_text, role_text)
    if role_text not in {"script", "storyboard", "subtitle", "analysis_report"}:
        role_text = "script"
    name_text = clean_public(name, limit=160, fallback=None) or "原始输入脚本"
    fmt_text = clean_public(fmt, limit=32, fallback=None)
    if not fmt_text:
        fmt_text = (suffix.lstrip(".") or "markdown").lower()
    # Keep format metadata descriptive rather than executable/path-like.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,31}", fmt_text):
        fmt_text = suffix.lstrip(".") or "markdown"
    return {"text": text, "role": role_text, "name": name_text, "format": fmt_text}


def _script_value(task: dict[str, Any]) -> tuple[Any, str | None]:
    """Find the preferred public script value and file reference."""
    for key in SCRIPT_FILE_KEYS:
        value = task.get(key)
        if isinstance(value, str) and value.strip():
            return None, value.strip()
    for key in SCRIPT_INLINE_KEYS:
        if key in task and task.get(key) is not None:
            return task.get(key), None
    return None, None


def read_public_script(task: dict[str, Any], task_file: Path | None = None) -> dict[str, str] | None:
    """Read a public input script from a task without exposing private prompts."""
    value, file_ref = _script_value(task)
    suffix = ""
    if file_ref:
        candidate = approved_local_file(file_ref, task, task_file, SCRIPT_EXTS)
        if candidate is None:
            # A path-shaped value that cannot be approved is not treated as
            # script text; this prevents absolute paths from leaking into the
            # project as a document body.
            return None
        try:
            if candidate.stat().st_size > MAX_SCRIPT_BYTES:
                return None
            raw = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        suffix = candidate.suffix.lower()
        value = raw
    if value is None:
        return None
    # If a producer puts a path in an inline alias, only accept it when the
    # path resolves inside an approved task root; otherwise do not echo it.
    if isinstance(value, str):
        candidate = approved_local_file(value, task, task_file, SCRIPT_EXTS)
        if candidate is not None and ("\n" not in value and "\r" not in value):
            try:
                if candidate.stat().st_size > MAX_SCRIPT_BYTES:
                    return None
                value = candidate.read_text(encoding="utf-8")
                suffix = candidate.suffix.lower()
            except (OSError, UnicodeError):
                return None
        elif _looks_like_local_path_reference(value) and "\n" not in value and "\r" not in value:
            return None
    return public_script_info(
        value,
        name=task_value(task, *SCRIPT_NAME_KEYS),
        role=task_value(task, *SCRIPT_ROLE_KEYS),
        fmt=task_value(task, *SCRIPT_FORMAT_KEYS),
        suffix=suffix,
    )


def local_video(task: dict[str, Any], task_file: Path | None = None) -> tuple[Path | None, str | None]:
    saw_value = False
    for value in iter_video_references(task):
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        # A remote URL/object key belongs to the remote cache path.  Do not
        # report it as a missing local file before that path gets a chance.
        if _remote_candidates(value) and not _looks_like_local_path_reference(value):
            continue
        saw_value = True
        resolved = approved_local_file(
            value,
            task,
            task_file,
            VIDEO_EXTS,
            allow_video_signature=True,
        )
        if resolved is not None:
            return resolved, None
        if re.match(r"^(?:https?|s3|data):", value.strip(), flags=re.I):
            return None, "视频地址尚未缓存到本地。"
    if not saw_value:
        return None, "视频结果未缓存到本地。"
    return None, "视频结果未缓存到本地。"


def resolve_video(
    task: dict[str, Any],
    task_file: Path | None,
    stage_dir: Path,
    task_id: str,
) -> tuple[Path | None, str | None, str]:
    """Resolve a local file first, then cache a provider remote URL if present."""

    local, local_error = local_video(task, task_file)
    if local is not None:
        return local, None, "local"
    reference = remote_video_reference(task)
    if reference:
        cached, remote_error = cache_remote_video(reference, stage_dir, task_id)
        if cached is not None:
            return cached, None, "remote"
        return None, remote_error or "远程视频暂时无法缓存。", "remote"
    return None, local_error or "视频文件不可用。", "none"


def existing_task_summary(path: Path) -> dict[str, Any] | None:
    value = jload(path)
    return value if isinstance(value, dict) else None


def existing_digest_version(project: P, clip: dict[str, Any], digest: str) -> dict[str, Any] | None:
    for version in clip.get("versions") or []:
        if str(version.get("sha256") or "") == "sha256:" + digest:
            return version
        rel = norm_rel(version.get("file"))
        if rel:
            file_path = Path(project.dir) / rel
            try:
                if not file_path.is_symlink() and project.rel_ok(rel) and file_path.is_file() and digest_file(file_path) == digest:
                    return version
            except OSError:
                pass
    return None


def clip_for(document: dict[str, Any], clip_id: str) -> dict[str, Any]:
    for clip in document.get("clips", []):
        if isinstance(clip, dict) and clip.get("id") == clip_id:
            return clip
    # A handoff normally follows a plan, but accepting a missing plan makes a
    # completed result recoverable without inventing generation work.
    clip = {
        "id": clip_id,
        "segment": None,
        "title": clip_id,
        "duration": None,
        "ratio": "9:16",
        "clarity": "Standard",
        "mappings": [],
        "preserve": None,
        "status": "planned",
        "outcome": None,
        "current": None,
        "versions": [],
        "poster": None,
        "proxy": None,
        "filmstrip": None,
        "revision_note": None,
        # ``handoff_*`` describes the most recent generator->manager
        # transport result.  It is deliberately separate from ``status``:
        # a failed/partial handoff must not invent a new clip state (and in
        # particular must not make a delivered clip look failed).
        "handoff_status": None,
        "handoff_message": None,
        "handoff_task_id": None,
        "handoff_updated": None,
    }
    document.setdefault("clips", []).append(clip)
    return clip


def update_placeholder_handoff(
    document: dict[str, Any],
    clip_id: str,
    *,
    status: str,
    message: str,
    task_id: str,
) -> bool:
    """Record a failed/partial handoff on an existing planned clip.

    ``vpm_prepare`` creates a clip before a generator runs.  If the result is
    failed or only partially delivered, the manager should surface that fact
    on the reserved clip instead of creating a second placeholder (or
    changing the legal clip status to an unsupported ``failed`` value).

    This helper intentionally does *not* call :func:`clip_for`: an unplanned
    failed task remains a task/state record only, preserving the historical
    video-first behavior and avoiding phantom projects.  Delivered and
    superseded clips are immutable with respect to a later failed result.
    """

    if not isinstance(clip_id, str) or not clip_id:
        return False
    candidate: dict[str, Any] | None = None
    for item in document.get("clips", []):
        if isinstance(item, dict) and item.get("id") == clip_id:
            candidate = item
            break
    if candidate is None:
        return False

    current = str(candidate.get("status") or "").strip().lower()
    if current in {"delivered", "superseded"}:
        # A late failed provider envelope must never downgrade a good version.
        return False

    safe_status = status if status in {"partial", "failed"} else "partial"
    safe_message = clean_public(
        message,
        limit=500,
        fallback="视频交接未完成。",
    ) or "视频交接未完成。"
    safe_task = safe_identifier(task_id, "task", TASK_ID_RE)
    desired: dict[str, Any] = {
        "handoff_status": safe_status,
        "handoff_message": safe_message,
        "handoff_task_id": safe_task,
        "handoff_updated": now(),
        # The current web application already renders revision_note.  Keep it
        # as a compatibility projection while the explicit handoff fields
        # give generator-neutral consumers a stable API.
        "revision_note": safe_message,
    }
    changed = any(candidate.get(key) != value for key, value in desired.items())
    if changed:
        candidate.update(desired)
    return changed


def generation_completion_confirmed(task: dict[str, Any]) -> bool:
    """Return whether the worker confirmed rendering before delivery failed.

    Newer generator task files carry the explicit boolean. Older files may only
    have the completed generation marker and terminal video status, so accept
    those conservative, observable signals as well.  This helper deliberately
    does not inspect provider names, URLs, or diagnostic payloads.
    """

    marker = task.get("generation_completion_confirmed")
    if isinstance(marker, bool):
        if marker:
            return True
    elif isinstance(marker, (int, float)) and marker == 1:
        return True
    elif isinstance(marker, str) and marker.strip().lower() in {"1", "true", "yes", "confirmed", "complete", "completed"}:
        return True
    generation = str(task_value(task, "generation_status") or "").strip().lower()
    if generation not in {"complete", "completed", "success", "succeeded", "done"}:
        return False
    terminal = task_value(task, "upstream_terminal_status")
    last_video = task_value(task, "upstream_last_video_status", "video_status")
    # An upstream video ID alone only means that generation started; it is not
    # proof that rendering reached a terminal state.  Require an observable
    # terminal success marker when the explicit boolean is absent.
    return terminal in (1, "1") or last_video in (100, "100")


def delivery_failure(task: dict[str, Any]) -> bool:
    """Whether a task failed after generation at a delivery boundary."""

    status = str(task_value(task, "delivery_status") or "").strip().lower()
    stage = str(task_value(task, "failure_stage") or "").strip().lower()
    return status in DELIVERY_FAILURE_STATUSES or stage in DELIVERY_FAILURE_STAGES


def generated_delivery_message(task: dict[str, Any], *, video_error: str | None = None) -> str:
    """Build a short, provider-neutral message for a confirmed render.

    The manager cannot expose a signed/object-store URL and cannot fabricate a
    local copy.  Say exactly that the render completed and the handoff did not,
    without naming a container such as MP4 as if it were a requirement.
    """

    if video_error and "暂存目录" in video_error:
        return "视频已生成，但交接暂存未完成，暂时没有本地视频文件。"
    if delivery_failure(task):
        return DELIVERY_UNAVAILABLE_MESSAGE
    return DELIVERY_INCOMPLETE_MESSAGE


def public_failure_message(task: dict[str, Any]) -> str:
    """Return a concise failure reason without leaking generic internals."""

    reason = clean_public(task_value(task, "failure_reason", "error"), limit=500)
    if reason:
        normalized = re.sub(r"[\s.!！。]+$", "", reason.casefold())
        if not any(
            normalized == generic or normalized.startswith(generic + " ")
            for generic in GENERIC_FAILURE_REASONS
        ):
            return reason
    stage = str(task_value(task, "failure_stage") or "").strip().lower()
    return FAILURE_STAGE_MESSAGES.get(stage, "视频任务未能完成。")


def public_status(task: dict[str, Any]) -> tuple[str, str]:
    raw = str(task_value(task, "status", "task_status") or "").lower().strip()
    generated = generation_completion_confirmed(task)
    if raw in {"complete", "completed", "success", "succeeded", "done"}:
        return "completed", "视频结果已接收。"
    if raw in {"failed", "error", "cancelled", "canceled", "timed_out", "timeout"}:
        # A generator can mark the outer task failed after rendering completed (for
        # example, when signing or caching the final link fails).  Preserve
        # that distinction for the manager as an incomplete delivery.
        if generated and (delivery_failure(task) or not task_value(task, "video_url", "video_file")):
            return "partial", generated_delivery_message(task)
        return "failed", public_failure_message(task)
    if raw in {"submitted", "processing", "queued", "running", "pending", "partial"}:
        if generated:
            return "partial", generated_delivery_message(task)
        return "partial", "视频任务尚未完成，暂未接收视频。"
    if generated:
        return "partial", generated_delivery_message(task)
    return "failed", "视频任务结果无法确认。"


def _scripts_fingerprint(document: dict[str, Any]) -> str:
    """Return a stable in-memory fingerprint for the public script list."""

    try:
        value = document.get("asset", {}).get("scripts", [])
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, AttributeError):
        return repr(document.get("asset", {}).get("scripts", []))


def _script_file_digest(project: P, rel: Any) -> str | None:
    """Hash an existing public script after removing the recorder newline."""

    path = norm_rel(rel)
    if not path or not project.rel_ok(path):
        return None
    candidate = Path(project.dir) / path
    try:
        if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > MAX_SCRIPT_BYTES:
            return None
        return hashlib.sha256(candidate.read_text(encoding="utf-8").strip().encode("utf-8")).hexdigest()
    except (OSError, UnicodeError):
        return None


def register_report(document: dict[str, Any], project: P, report_text: str, clip_id: str, task_id: str) -> str:
    report_rel = f"assets/reports/{clip_id}-retention.md"
    report_path = Path(project.dir) / report_rel
    if not project.rel_ok(report_rel) or report_path.is_symlink():
        fail("分析报告路径不可用。")
    desired_text = report_text.rstrip() + "\n"
    try:
        current_text = report_path.read_text(encoding="utf-8") if report_path.is_file() and not report_path.is_symlink() else None
    except (OSError, UnicodeError):
        current_text = None
    if current_text != desired_text:
        atomic_text(report_path, desired_text)
    report_id = stable_id("scr", report_rel, clip_id)
    scripts = document.setdefault("asset", {}).setdefault("scripts", [])
    prior_entry = next((item for item in scripts
                        if isinstance(item, dict) and item.get("id") == report_id), None)
    entry = {
        "id": report_id,
        "role": "analysis_report",
        "file": report_rel,
        "name": f"{clip_id} retention report",
        "format": "markdown",
        "related_clips": [clip_id],
        "status": "active",
        "created": (prior_entry.get("created") if isinstance(prior_entry, dict) else None) or now(),
        "task_id": task_id,
    }
    if prior_entry != entry:
        scripts[:] = [item for item in scripts if not (isinstance(item, dict) and item.get("id") == report_id)]
        scripts.append(entry)
    return report_rel


def register_script(document: dict[str, Any], project: P, script_info: dict[str, str],
                    clip_id: str, task_id: str) -> tuple[str, str]:
    """Persist one public generation input and register it in asset.scripts.

    The content digest is the identity, so rescanning the same handoff (or
    linking the same brief to another clip) updates relationships without
    creating duplicate files.  A changed input gets a new append-only public
    document and never overwrites the prior script.
    """
    text = str(script_info.get("text") or "").strip()
    role = str(script_info.get("role") or "script").strip().lower()
    if role not in {"script", "storyboard", "subtitle", "analysis_report"}:
        role = "script"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    script_id = stable_id("scr", role, digest)
    scripts = document.setdefault("asset", {}).setdefault("scripts", [])
    existing = next((item for item in scripts
                     if isinstance(item, dict) and item.get("id") == script_id), None)
    # Older manager versions did not persist ``sha256`` on script records and
    # may have chosen a different filename for the same public text.  Reuse a
    # matching public file so a formatting-only handoff cannot create another
    # card in the web application.
    if not isinstance(existing, dict):
        for candidate in scripts:
            if not isinstance(candidate, dict) or str(candidate.get("role") or "script") != role:
                continue
            if _script_file_digest(project, candidate.get("file")) == digest:
                existing = candidate
                script_id = str(candidate.get("id") or script_id)
                break
    if isinstance(existing, dict) and isinstance(existing.get("file"), str):
        script_rel = norm_rel(existing.get("file"))
    else:
        # Keep filenames short and independent of untrusted task/project text.
        extension = str(script_info.get("format") or "markdown").strip().lower()
        ext = ".md" if extension in {"md", "markdown", "text", "txt"} else "." + re.sub(r"[^a-z0-9]", "", extension)[:8]
        if ext == ".":
            ext = ".md"
        if ext not in SCRIPT_EXTS:
            ext = ".md"
        script_rel = f"assets/scripts/input-{digest[:24]}{ext}"
    if not script_rel or not project.rel_ok(script_rel):
        fail("脚本路径不可用。")
    script_path = Path(project.dir) / script_rel
    if script_path.is_symlink():
        fail("脚本路径不可用。")
    # Atomic replacement is safe for a known project-relative regular file;
    # content identity ensures this never changes a different script.
    desired_text = text.rstrip() + "\n"
    try:
        current_text = script_path.read_text(encoding="utf-8") if script_path.is_file() and not script_path.is_symlink() else None
    except (OSError, UnicodeError):
        current_text = None
    if current_text != desired_text:
        atomic_text(script_path, desired_text)
    related: list[str] = []
    if isinstance(existing, dict) and isinstance(existing.get("related_clips"), list):
        related.extend(item for item in existing["related_clips"] if isinstance(item, str))
    if clip_id and clip_id not in related:
        related.append(clip_id)
    entry = {
        "id": script_id,
        "role": role,
        "file": script_rel,
        "name": clean_public(script_info.get("name"), limit=160, fallback="原始输入脚本") or "原始输入脚本",
        "format": clean_public(script_info.get("format"), limit=32, fallback="markdown") or "markdown",
        "related_clips": related,
        "status": "active",
        "created": (existing.get("created") if isinstance(existing, dict) else None) or now(),
        "sha256": "sha256:" + digest,
    }
    scripts[:] = [item for item in scripts if not (isinstance(item, dict) and item.get("id") == script_id)]
    scripts.append(entry)
    return script_rel, script_id


def handoff(root: Path, slug: str, task_file: Path, clip_arg: str | None) -> dict[str, Any]:
    if not SLUG_RE.fullmatch(slug) or slug in {"webapp", "trash", "api", "staging"}:
        fail("项目 slug 无效。")
    if not task_file.is_file():
        fail("task 文件不存在。")
    try:
        if task_file.is_symlink() or task_file.stat().st_size > MAX_TASK_BYTES:
            fail("task 文件不可处理。")
    except OSError:
        fail("task 文件不可处理。")
    task = jload(task_file)
    if not isinstance(task, dict):
        fail("task 文件不是有效 JSON。")

    task_raw_id = task_value(task, "task_id", "id") or task_file.stem
    task_id = safe_identifier(task_raw_id, "task", TASK_ID_RE)
    clip_raw = clip_arg or task_value(task, "clip_id", "clip", "segment_id")
    clip_id = safe_identifier(clip_raw or stable_id("clip", task_id), "clip", CLIP_ID_RE)
    status, default_message = public_status(task)
    initial_status = status
    generation_confirmed = generation_completion_confirmed(task)
    # Resolve the local output after the project is loaded so a remote-only
    # result can be cached inside its protected staging directory.
    video, video_error = local_video(task, task_file)
    remote_reference = remote_video_reference(task)
    report_text, report_error = read_public_report(task, task_file)
    script_info = read_public_script(task, task_file)

    video_digest = digest_file(video) if video else None
    report_digest = hashlib.sha256(report_text.encode("utf-8")).hexdigest() if report_text else None
    script_digest = hashlib.sha256(str(script_info.get("text") or "").encode("utf-8")).hexdigest() if script_info else None
    remote_digest = hashlib.sha256(remote_reference.encode("utf-8")).hexdigest() if remote_reference else None
    idem_raw = "\x1f".join(str(x or "") for x in (task_id, status, clip_id, video_digest, remote_digest, report_digest, script_digest))
    idempotency = "sha256:" + hashlib.sha256(idem_raw.encode("utf-8")).hexdigest()

    project = P(str(root), slug)
    document = project.load()
    task_dir = Path(project.dir) / "tasks"
    if not project.rel_ok("tasks") or task_dir.is_symlink():
        fail("项目任务目录不可用。")
    task_path = task_dir / f"{task_id}.json"
    prior = existing_task_summary(task_path)
    if prior and prior.get("idempotency") == idempotency:
        # A completed handoff is immutable.  A partial/failed handoff can be
        # retried only when the source still contains a remote reference; a
        # reference-less result has no new input for the manager to recover.
        prior_status = str(prior.get("status") or "").strip().lower()
        retryable_source = bool(remote_reference) or video is not None
        # Older manager versions did not extract audio and therefore have no
        # ``audio`` field in their completed task summary.  Allow one repair
        # pass when the source is still available; the new summary records the
        # outcome so subsequent scans remain idempotent.
        audio_repair_needed = (
            prior_status == "completed"
            and retryable_source
            and not isinstance(prior.get("audio"), dict)
        )
        if ((prior_status == "completed" and not audio_repair_needed)
                or (prior_status in {"partial", "failed"} and not retryable_source)):
            return {"ok": True, "idempotent": True, "task_id": task_id,
                    "status": prior.get("status", status), "clip_id": prior.get("clip_id", clip_id),
                    "outputs": prior.get("outputs", []), "report": prior.get("report"),
                    "script": prior.get("script"), "message": prior.get("message") or default_message,
                    "audio": prior.get("audio")}

    generator = generator_name(task)
    stage_dir = Path(project.dir) / "staging" / generator / task_id
    if video is None and remote_reference:
        if not project.rel_ok(f"staging/{generator}/{task_id}") or stage_dir.is_symlink():
            video_error = "交接暂存目录不可用。"
        else:
            video, remote_error, _origin = resolve_video(task, task_file, stage_dir, task_id)
            video_error = remote_error if video is None else None
            if video is not None:
                video_digest = digest_file(video)

    outputs: list[dict[str, Any]] = []
    audio_result: dict[str, str] | None = None
    task_message = clean_public(task_value(task, "message", "summary", "user_summary"), limit=500)
    message = public_failure_message(task) if status == "failed" else (task_message or default_message)
    report_rel: str | None = None
    script_rel: str | None = None

    # A complete task is only successful once a usable local copy is present;
    # remote URLs are cached above so signed/object-store links never become
    # part of the project manifest.
    if generation_confirmed and video is not None and status != "completed":
        # Older workers sometimes left the outer task as failed after the
        # render completed.  Once the manager has a verified local/cacheable
        # file, the handoff itself is a completed delivery.
        status = "completed"
        message = task_message or ("视频已接收并缓存。" if remote_reference else "视频已接收。")
    elif status == "completed" and video is None:
        status = "partial"
        message = (
            generated_delivery_message(task, video_error=video_error)
            if generation_confirmed
            else video_error or "视频结果无法在本地取得。"
        )
    elif generation_confirmed and video is None:
        # Preserve the upstream fact that rendering completed even when no
        # usable local file or remote reference survived into the task file.
        status = "partial"
        message = generated_delivery_message(task, video_error=video_error)
    elif status == "completed" and video is not None:
        message = task_message or ("视频已接收并缓存。" if remote_reference else "视频已接收。")

    changed = False
    delivered_video_rel: str | None = None
    if video is not None and status == "completed":
        clip = clip_for(document, clip_id)
        duplicate = existing_digest_version(project, clip, video_digest or "")
        if duplicate:
            version = duplicate
            delivered_video_rel = norm_rel(version.get("file"))
            # Re-scanning an already-delivered handoff must not undo a user's
            # selected version in the management page.  Only repair
            # ``current`` when it is missing/invalid or the clip has not yet
            # reached a delivered state.
            selected_version_valid = False
            for selected in clip.get("versions") or []:
                if not isinstance(selected, dict):
                    continue
                if str(selected.get("v")) != str(clip.get("current")):
                    continue
                selected_rel = norm_rel(selected.get("file"))
                if not selected_rel or not project.rel_ok(selected_rel):
                    continue
                selected_path = Path(project.dir) / selected_rel
                try:
                    selected_version_valid = selected_path.is_file() and not selected_path.is_symlink()
                except OSError:
                    selected_version_valid = False
                if selected_version_valid:
                    break
            repair_current = (
                clip.get("status") not in {"delivered", "superseded"}
                or not selected_version_valid
            )
            # A prior scan may have copied the bytes but been interrupted
            # before the manifest was finalized.  Repair that projection
            # without appending a second version.
            if ((repair_current and clip.get("current") != version.get("v"))
                    or clip.get("status") != "delivered"
                    or clip.get("revision_note") is not None
                    or any(clip.get(key) is not None for key in (
                        "handoff_status", "handoff_message", "handoff_task_id", "handoff_updated"))):
                updates = {
                    "status": "delivered",
                    "revision_note": None,
                    "handoff_status": None, "handoff_message": None,
                    "handoff_task_id": None, "handoff_updated": None,
                }
                if repair_current:
                    updates["current"] = version.get("v")
                if clip.get("current") != version.get("v") or clip.get("status") != "delivered":
                    updates["outcome"] = "complete"
                clip.update(updates)
                changed = True
        else:
            # Stage first, then copy into the append-only clip version path.
            stage_dir = Path(project.dir) / "staging" / generator / task_id
            if not project.rel_ok(f"staging/{generator}/{task_id}") or stage_dir.is_symlink():
                fail("交接暂存目录不可用。")
            stage_dir.mkdir(parents=True, exist_ok=True)
            suffix = video.suffix.lower() if video.suffix.lower() in VIDEO_EXTS else ".mp4"
            staged = stage_dir / f"video{suffix}"
            fd, stage_temp = tempfile.mkstemp(prefix=".handoff-stage.", suffix=suffix, dir=str(stage_dir))
            os.close(fd)
            try:
                shutil.copy2(video, stage_temp)
                os.replace(stage_temp, staged)
            finally:
                try:
                    os.unlink(stage_temp)
                except OSError:
                    pass
            versions = clip.setdefault("versions", [])
            next_v = max((int(item.get("v", 0)) for item in versions), default=0) + 1
            rel = f"clips/{clip_id}/v{next_v}{suffix}"
            destination = Path(project.dir) / rel
            if not project.rel_ok(rel) or destination.exists() or destination.is_symlink():
                fail("片段版本路径不可用。")
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Copy to a temporary sibling then atomically replace the new path;
            # this prevents a pre-created symlink from redirecting the copy.
            fd, temporary = tempfile.mkstemp(prefix=".handoff.", suffix=suffix, dir=str(destination.parent))
            os.close(fd)
            try:
                shutil.copy2(staged, temporary)
                os.replace(temporary, destination)
            finally:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            version = {"v": next_v, "file": rel, "created": now(),
                        "note": (f"{generator} handoff (remote cached)" if remote_reference else f"{generator} handoff"), "superseded": False,
                       "sha256": "sha256:" + (video_digest or "")}
            # Record the measured duration when the local media probe is
            # available.  This is metadata only; a probe failure must never
            # turn a successfully copied video into a failed handoff.
            measured_duration = probe_media_duration(destination)
            if measured_duration is not None:
                version["duration"] = round(measured_duration, 6)
                clip["duration"] = round(measured_duration, 6)
            delivered_video_rel = rel
            for old in versions:
                old["superseded"] = True
            versions.append(version)
            clip.update({"current": next_v, "status": "delivered", "outcome": "complete", "revision_note": None,
                         "handoff_status": None, "handoff_message": None,
                         "handoff_task_id": None, "handoff_updated": None})
            for key, value in derivatives(project, clip_id, rel).items():
                clip[key] = value
            remaining = [item for item in document["clips"] if item.get("status") not in {"delivered", "superseded"}]
            document["status"] = "generating" if remaining else "reviewing"
            changed = True
        outputs.append({"file": version["file"], "kind": "clip", "sha256": "sha256:" + (video_digest or "")})
        # Keep audio extraction independent from the generator and fail-open:
        # a video remains a successful delivery even when it has no audio
        # stream or ffmpeg cannot transcode it on this machine.
        if delivered_video_rel:
            audio_rel, audio_error = extract_audio_track(
                project,
                delivered_video_rel,
                clip_id,
                video_digest,
            )
            if audio_rel:
                audio_entry, audio_changed = register_extracted_audio(
                    document,
                    project,
                    audio_rel=audio_rel,
                    clip_id=clip_id,
                    source_rel=delivered_video_rel,
                    video_digest=video_digest or "",
                )
                if audio_entry:
                    audio_digest = str(audio_entry.get("hash") or "")
                    audio_result = {"status": "registered", "file": audio_rel}
                    outputs.append({
                        "file": audio_rel,
                        "kind": "audio",
                        "sha256": audio_digest,
                        "origin": "generated",
                    })
                    changed = changed or audio_changed
                else:
                    audio_result = {
                        "status": "unavailable",
                        "message": "音频记录未完成。",
                    }
            else:
                # Keep a short, user-visible warning in the sanitized task
                # summary while preserving the successful video status.
                audio_result = {
                    "status": "unavailable",
                    "message": clean_public(
                        audio_error,
                        limit=160,
                        fallback="音频轨道未提取。",
                    ) or "音频轨道未提取。",
                }
    elif video_error and initial_status == "partial" and not generation_confirmed:
        message = clean_public(video_error, limit=500, fallback=message) or message

    if report_text and status == "completed" and video is not None:
        # A report is public only when this handoff has a usable video.  A
        # failed/partial task may carry stale report text, but exposing it
        # without the corresponding video would violate the generator delivery
        # contract.
        before_scripts = _scripts_fingerprint(document)
        report_rel = register_report(document, project, report_text, clip_id, task_id)
        report_digest = hashlib.sha256(report_text.encode("utf-8")).hexdigest()
        outputs.append({"file": report_rel, "kind": "report", "sha256": "sha256:" + report_digest})
        changed = changed or _scripts_fingerprint(document) != before_scripts
    elif report_error and status == "completed" and video is not None:
        message = "视频已接收；分析报告未返回。"

    if script_info and status == "completed" and video is not None:
        # The original/public input is a first-class script-chain resource;
        # private transformed prompts are filtered before this point.
        before_scripts = _scripts_fingerprint(document)
        script_rel, _script_id = register_script(document, project, script_info, clip_id, task_id)
        outputs.append({"file": script_rel, "kind": "script", "sha256": "sha256:" + (script_digest or "")})
        changed = changed or _scripts_fingerprint(document) != before_scripts

    if status in {"partial", "failed"}:
        # When preflight created a placeholder, attach the transport outcome
        # to that existing row.  This is intentionally a no-op for an
        # unplanned failed task and for delivered/superseded clips.
        changed = update_placeholder_handoff(
            document,
            clip_id,
            status=status,
            message=message,
            task_id=task_id,
        ) or changed

    if changed:
        project.save(document, "generator.handoff", {"task_id": task_id, "clip": clip_id, "status": status, "generator": generator})
    if status == "completed":
        project.state(
            phase=normalize_project(document, slug).get("status", "reviewing"),
            active_clip=None,
            queue=[],
            last_error={"clip": None, "message": None},
        )
    elif status in {"partial", "failed"}:
        # Keep the manager overview honest for both delivery problems after a
        # confirmed render and ordinary generation failures.  The state file
        # contains only the sanitized customer-facing sentence.
        project.state(last_error={"clip": clip_id, "message": message})

    summary = {
        "schema": 1,
        "task_id": task_id,
        "source": generator,
        "status": status,
        "received": now(),
        "clip_id": clip_id,
        "outputs": outputs,
        "report": report_rel,
        "script": script_rel,
        "message": clean_public(message, limit=500, fallback="交接结果已记录。"),
        "idempotency": idempotency,
    }
    if audio_result:
        summary["audio"] = audio_result
    jwrite(task_path, summary)
    sync_index(str(root))
    return {"ok": True, "idempotent": False, "task_id": task_id, "status": status,
            "clip_id": clip_id, "outputs": outputs, "report": report_rel, "script": script_rel,
            "message": summary["message"], "audio": audio_result}


def default_root() -> Path:
    """Resolve the manager root with the precedence the other entry points use."""

    explicit = os.environ.get("VIDEO_ASSET_MANAGER_ROOT") or os.environ.get("VPM_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
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
    if os.environ.get("CAPAFY_WORKSPACE"):
        return Path(os.environ["CAPAFY_WORKSPACE"]).expanduser().resolve() / ".capafy" / "video-asset-manager"
    return Path.home() / "workspace" / ".capafy" / "video-asset-manager"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None)
    parser.add_argument("--project", required=True)
    parser.add_argument("--task-file", required=True)
    parser.add_argument("--clip", help="clip id; otherwise read clip_id/clip from the task")
    args = parser.parse_args()
    try:
        explicit = args.root or os.environ.get("VIDEO_ASSET_MANAGER_ROOT") or os.environ.get("VPM_ROOT")
        if explicit:
            root = Path(explicit).expanduser().resolve()
        else:
            root = default_root()
        result = handoff(root, args.project, Path(args.task_file).expanduser().resolve(), args.clip)
    except Exception:
        # The generator integrations parse stdout as a single JSON object; a
        # raw traceback would lose the outcome entirely.  Keep stdout a
        # customer-safe JSON result and route the real diagnostics to stderr
        # so operators can still see what failed.
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "error": "视频交接失败。"}, ensure_ascii=False))
        return 1
    if not isinstance(result, dict):
        print(json.dumps({"ok": False, "error": "视频交接失败。"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
