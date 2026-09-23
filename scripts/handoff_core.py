#!/usr/bin/env python3
"""Generator-neutral handoff API.

This module exposes the shared API implemented in ``handoff_impl.py``.
Adapters import this surface to keep their dependency contract explicit.
"""
from __future__ import annotations

# Re-export the shared implementation without duplicating registration logic.
from handoff_impl import (  # noqa: F401,F403
    VIDEO_EXTS,
    ABSOLUTE_RE,
    PRIVATE_RE,
    SLUG_RE,
    TASK_ID_RE,
    CLIP_ID_RE,
    clean_public,
    generation_completion_confirmed,
    handoff,
    local_video,
    remote_video_reference,
    cache_remote_video,
    approved_local_file,
    approved_task_roots,
    SCRIPT_EXTS,
    SCRIPT_INLINE_KEYS,
    SCRIPT_FILE_KEYS,
    sanitize_script_text,
    public_script_info,
    iter_video_references,
    read_public_script,
    register_script,
    update_placeholder_handoff,
    extract_audio_track,
    register_extracted_audio,
    main,
)
