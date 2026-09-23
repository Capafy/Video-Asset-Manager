#!/usr/bin/env python3
"""Shared privacy helpers for the generator-neutral video manager.

The manager is a local bookkeeping/runtime process.  It must not inherit
provider credentials and must not persist transport secrets such as signed
object-store URLs.  Keep this module dependency-free so the same rules can be
used by the source skill, the installed webapp bundle, and completion bridges.
"""
from __future__ import annotations

import contextlib
import os
import re
from typing import Iterator, Mapping
from urllib.parse import urlsplit, urlunsplit


# Explicit names cover credentials used by video-generation providers and
# common object-storage/API clients.  The pattern below catches future
# provider-specific names without requiring an update to this list.
PRIVATE_ENV_NAMES = {
    "OPENAI_API_KEY",
    "ARK_API_KEY",
    "TOS_ACCESS_KEY",
    "TOS_SECRET_KEY",
    "TOS_SESSION_TOKEN",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "VIDEO_GENERATOR_API_KEY",
}

PRIVATE_ENV_RE = re.compile(
    r"(?:^|_)(?:API[_-]?KEY|ACCESS[_-]?KEY|SECRET(?:[_-]?KEY)?|"
    r"SESSION[_-]?TOKEN|(?:REFRESH[_-]?)?TOKEN|PASSWORD|PASSWD|"
    r"AUTH(?:ORIZATION)?|CREDENTIALS?|PRIVATE[_-]?KEY|SIGNATURE)(?:_|$)",
    re.I,
)

# These values are operational settings, not provider credentials, and are
# intentionally retained in the manager child environment.
SAFE_ENV_EXACT = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONUNBUFFERED",
    "CAPAFY_WORKSPACE", "VIDEO_ASSET_MANAGER_ROOT", "VPM_ROOT",
    "VIDEO_GENERATOR_HANDOFF_DIR", "VIDEO_GENERATOR_TASK_DIR",
    "VAM_HANDOFF_DIR", "VAM_OUTPUT_DIR", "VAM_PORT", "VPM_PORT",
    "VAM_BIND", "NO_PROXY", "NO_PROXY",
}
SAFE_ENV_PREFIXES = (
    "LC_", "LANG_", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "NO_PROXY", "VAM_", "VPM_", "CAPAFY_", "VIDEO_ASSET_MANAGER_",
    "VIDEO_GENERATOR_HANDOFF_",
)
PROXY_NAMES = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}


def is_private_env_name(name: object) -> bool:
    """Whether an environment variable name looks like a credential."""

    upper = str(name or "").strip().upper()
    if not upper or upper in SAFE_ENV_EXACT:
        return False
    # Credential markers take precedence over an operational prefix (for
    # example ``VAM_API_KEY`` must still be removed).
    if upper in PRIVATE_ENV_NAMES or bool(PRIVATE_ENV_RE.search(upper)):
        return True
    # Operational prefixes are explicitly allowlisted.  They are *not*
    # credential names; an earlier implementation returned the prefix match
    # itself, which accidentally stripped VAM_* settings (including the
    # loopback-test flag) and unauthenticated proxy configuration.
    if any(upper.startswith(p) for p in SAFE_ENV_PREFIXES):
        return False
    return False


def _proxy_without_userinfo(value: str) -> str | None:
    """Keep a proxy setting only when it has no embedded username/password."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text if "://" in text else "http://" + text)
    except ValueError:
        return None
    if parsed.username is not None or parsed.password is not None or not parsed.netloc:
        return None
    # Preserve the caller's original spelling for ordinary unauthenticated
    # proxies; no credentials are introduced by this normalization.
    return text


def manager_child_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a child environment stripped of credential-like variables.

    Workspace, locale, PATH, and unauthenticated proxy settings remain
    available.  The function is case-insensitive to match Windows environment
    semantics and never mutates the caller's mapping.
    """

    source = dict(os.environ if base is None else base)
    clean: dict[str, str] = {}
    for name, value in source.items():
        upper = str(name).upper()
        if is_private_env_name(upper):
            continue
        if upper in PROXY_NAMES:
            safe_proxy = _proxy_without_userinfo(value)
            if safe_proxy is None:
                continue
            clean[name] = safe_proxy
            continue
        clean[name] = value
    return clean


@contextlib.contextmanager
def sanitized_process_environment() -> Iterator[None]:
    """Temporarily apply the manager environment to in-process handoffs.

    Completion bridges can be called from a generator host process that still
    needs its credentials after the call returns.  This context protects the
    manager-side scanner/remote cache without permanently mutating that host.
    """

    original = dict(os.environ)
    safe = manager_child_environment(original)
    try:
        os.environ.clear()
        os.environ.update(safe)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


# Public text is customer-visible.  Reject URLs and common credential-shaped
# assignments even when a provider omitted a literal ``api_key`` field name.
PRIVATE_TEXT_RE = re.compile(
    r"(?:api[_-]?key|apikey|access[_-]?key|secret(?:[_-]?key)?|"
    r"session[_-]?token|refresh[_-]?token|password|passwd|authorization|"
    r"bearer\s+|private[_-]?key|signed[_-]?url|provider|model[_-]?route|"
    r"stack\s*trace|x-amz-(?:credential|signature|security-token)|"
    r"(?:^|\b)(?:sk|rk|akia|asia)-[A-Za-z0-9_-]{8,})",
    re.I,
)
REMOTE_URL_RE = re.compile(r"(?:https?|s3|file|data|ftp)://|(?<!\w)//[^\s]+", re.I)
ABSOLUTE_PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\|/(?:home|users|tmp|var|private|root|workspace|mnt|opt|srv|etc|run|proc|sys)(?:[\\/]|$))",
    re.I,
)


def contains_private_text(value: object, *, allow_public_url: bool = False) -> bool:
    text = str(value or "")
    if PRIVATE_TEXT_RE.search(text) or ABSOLUTE_PATH_RE.search(text):
        return True
    if not allow_public_url and REMOTE_URL_RE.search(text):
        return True
    return False


def sanitize_public_text(value: object, *, limit: int = 4000,
                         fallback: str | None = None,
                         allow_public_url: bool = False) -> str | None:
    """Return bounded public text or *fallback* when it looks private."""

    if value is None:
        return fallback
    text = str(value).replace("\x00", "").strip()
    if not text or len(text) > limit or contains_private_text(text, allow_public_url=allow_public_url):
        return fallback
    return text


def redact_remote_reference(value: object) -> str | None:
    """Strip query/fragment credentials from a transport URL for persistence.

    A plain public URL is retained for compatibility, while signed/object-store
    query parameters are removed.  The returned value is only a diagnostic
    hint; callers must still use the original URL in memory to download.
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https", "s3"} or not parsed.netloc:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    # Never persist a query/fragment, even if its parameter names are unusual.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def redact_recursive(value: object, *, depth: int = 0) -> object:
    """Best-effort public projection for logs/state/API fallback paths."""

    if depth > 12:
        return None
    if isinstance(value, dict):
        output: dict[str, object] = {}
        for key, child in value.items():
            key_text = str(key)
            if contains_private_text(key_text):
                continue
            clean = redact_recursive(child, depth=depth + 1)
            if clean is not None:
                output[key_text] = clean
        return output
    if isinstance(value, list):
        return [clean for child in value
                if (clean := redact_recursive(child, depth=depth + 1)) is not None]
    if isinstance(value, str):
        return sanitize_public_text(value, limit=2_000_000) 
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return None
