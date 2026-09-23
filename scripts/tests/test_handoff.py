#!/usr/bin/env python3
"""Fixture tests for the generator-to-manager handoff.

These tests use only local task JSON and small fixture files.  They never call
the generation service (or any network endpoint); remote-cache behavior is
covered by the pure classification assertions and is intentionally not
enabled here.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import handoff_impl as handoff_module  # noqa: E402
import vpm_sync as sync_module  # noqa: E402
from vpm_record import P, empty_project, ensure_dirs, jload, jwrite  # noqa: E402


class HandoffFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="vam-handoff-fixture-")
        self.root = Path(self.temp.name) / "manager"
        self.task_dir = Path(self.temp.name) / "tasks"
        self.task_dir.mkdir(parents=True)
        self.slug = "fixture-project"
        project = P(str(self.root), self.slug)
        ensure_dirs(project.dir)
        jwrite(project.pj, empty_project(self.slug, "Fixture project", "generate"))
        (self.root / "active_project").parent.mkdir(parents=True, exist_ok=True)
        (self.root / "active_project").write_text(self.slug, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_task(self, name: str, payload: dict) -> Path:
        path = self.task_dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def read_summary(self, task_id: str) -> dict:
        return jload(self.root / self.slug / "tasks" / f"{task_id}.json")

    def test_completed_generation_with_signing_failure_is_partial_and_idempotent(self) -> None:
        task = {
            "task_id": "task_signing_fixture",
            "status": "failed",
            "generation_status": "completed",
            "generation_completion_confirmed": True,
            "delivery_status": "signing_failed",
            "failure_stage": "video_link_signing",
            "failure_reason": "The video service rejected the request",
            "error": "The video was generated, but its delivery link could not be prepared.",
        }
        task_file = self.write_task("task_signing_fixture.json", task)

        result = handoff_module.handoff(self.root, self.slug, task_file, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertIn("视频已生成", result["message"])
        self.assertIn("交付链接不可用", result["message"])
        self.assertIn("本地视频文件", result["message"])
        self.assertNotIn("The video service rejected", result["message"])
        summary = self.read_summary("task_signing_fixture")
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["message"], result["message"])
        self.assertEqual(summary["outputs"], [])
        self.assertEqual(jload(self.root / self.slug / "project.json")["clips"], [])
        state = jload(self.root / self.slug / "state.json")
        self.assertIn("视频已生成", state["last_error"]["message"])

        repeated = handoff_module.handoff(self.root, self.slug, task_file, None)
        self.assertTrue(repeated["ok"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["status"], "partial")
        self.assertEqual(repeated["message"], result["message"])

    def test_completed_generation_with_local_file_is_delivered_once(self) -> None:
        video = self.task_dir / "fixture.webm"
        # local_video only needs an approved, existing video path; the bytes
        # make the fixture large enough for future media validation as well.
        video.write_bytes(b"\x00" * 2048)
        task = {
            "task_id": "task_local_fixture",
            "clip_id": "clip_fixture",
            "status": "failed",
            "generation_status": "completed",
            "generation_completion_confirmed": True,
            "delivery_status": "signing_failed",
            "failure_stage": "video_link_signing",
            "video_file": video.name,
            "report": "# Retention\n\nHook is clear.",
        }
        task_file = self.write_task("task_local_fixture.json", task)

        # ffmpeg derivatives are outside this fixture's scope and would make
        # the test depend on an installed binary.
        with mock.patch.object(handoff_module, "derivatives", return_value={}), \
             mock.patch.object(handoff_module, "probe_media_duration", return_value=4.096):
            result = handoff_module.handoff(self.root, self.slug, task_file, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["outputs"]), 2)
        self.assertEqual(result["outputs"][0]["file"], "clips/clip_fixture/v1.webm")
        self.assertEqual(result["report"], "assets/reports/clip_fixture-retention.md")
        manifest = jload(self.root / self.slug / "project.json")
        clip = next(item for item in manifest["clips"] if item["id"] == "clip_fixture")
        self.assertEqual(clip["status"], "delivered")
        self.assertEqual(len(clip["versions"]), 1)
        self.assertEqual(clip["duration"], 4.096)
        self.assertEqual(clip["versions"][0]["duration"], 4.096)
        self.assertTrue((self.root / self.slug / "clips/clip_fixture/v1.webm").is_file())
        self.assertEqual(len(manifest["asset"]["scripts"]), 1)
        state = jload(self.root / self.slug / "state.json")
        self.assertIsNone(state["last_error"]["message"])

        with mock.patch.object(handoff_module, "derivatives", return_value={}):
            repeated = handoff_module.handoff(self.root, self.slug, task_file, None)
        self.assertTrue(repeated["idempotent"])
        manifest_again = jload(self.root / self.slug / "project.json")
        clip_again = next(item for item in manifest_again["clips"] if item["id"] == "clip_fixture")
        self.assertEqual(len(clip_again["versions"]), 1)

    def test_generated_video_audio_is_extracted_and_marked_ai_generated(self) -> None:
        """A delivered AV file gets one discoverable generated-audio asset."""

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg is not installed")
        video = self.task_dir / "fixture-with-audio.mp4"
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:r=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "1",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(video),
        ]
        generated = subprocess.run(command, capture_output=True, timeout=60)
        if generated.returncode != 0 or not video.is_file():
            self.skipTest("ffmpeg cannot create the AV fixture")

        task_file = self.write_task("task_audio_fixture.json", {
            "task_id": "task_audio_fixture",
            "clip_id": "clip_audio",
            "status": "completed",
            "generation_status": "completed",
            "generation_completion_confirmed": True,
            "video_file": video.name,
        })

        with mock.patch.object(handoff_module, "derivatives", return_value={}):
            result = handoff_module.handoff(self.root, self.slug, task_file, None)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["audio"]["status"], "registered")
        audio_outputs = [item for item in result["outputs"] if item.get("kind") == "audio"]
        self.assertEqual(len(audio_outputs), 1)
        self.assertTrue(audio_outputs[0]["file"].startswith("assets/generated/audio-clip_audio-"))
        manifest = jload(self.root / self.slug / "project.json")
        audio = [item for item in manifest["asset"]["media"] if item.get("kind") == "audio"]
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0]["origin"], "generated")
        self.assertIn("ai-generated", audio[0]["tags"])
        self.assertIn("clip_audio", audio[0]["used_by"])
        audio_path = self.root / self.slug / audio[0]["file"]
        self.assertTrue(audio_path.is_file())
        self.assertGreater(audio_path.stat().st_size, 128)
        sidecar = self.root / self.slug / audio[0]["gen"]["sidecar"]
        self.assertTrue(sidecar.is_file())
        self.assertEqual(jload(sidecar)["model_label"], "audio")

        # Simulate a task summary written by an older manager build before
        # audio extraction existed.  A later scan should backfill the same
        # deterministic record instead of treating the completed handoff as
        # permanently immutable.
        summary_path = self.root / self.slug / "tasks" / "task_audio_fixture.json"
        legacy_summary = jload(summary_path)
        legacy_summary.pop("audio", None)
        jwrite(summary_path, legacy_summary)
        with mock.patch.object(handoff_module, "derivatives", return_value={}):
            repaired = handoff_module.handoff(self.root, self.slug, task_file, None)
        self.assertFalse(repaired["idempotent"])
        self.assertEqual(repaired["audio"]["status"], "registered")

        with mock.patch.object(handoff_module, "derivatives", return_value={}):
            repeated = handoff_module.handoff(self.root, self.slug, task_file, None)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["audio"]["status"], "registered")
        manifest_again = jload(self.root / self.slug / "project.json")
        self.assertEqual(
            len([item for item in manifest_again["asset"]["media"] if item.get("kind") == "audio"]),
            1,
        )

    def test_audio_extraction_failure_is_fail_open_for_video_delivery(self) -> None:
        video = self.task_dir / "fixture-without-audio.webm"
        video.write_bytes(b"\x1a\x45\xdf\xa3" + b"V" * 4096)
        task_file = self.write_task("task_audio_fail_open.json", {
            "task_id": "task_audio_fail_open",
            "clip_id": "clip_audio_fail_open",
            "status": "completed",
            "generation_status": "completed",
            "generation_completion_confirmed": True,
            "video_file": video.name,
        })

        with mock.patch.object(handoff_module, "derivatives", return_value={}), \
                mock.patch.object(handoff_module, "extract_audio_track", return_value=(None, "ffmpeg unavailable")):
            result = handoff_module.handoff(self.root, self.slug, task_file, None)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["audio"]["status"], "unavailable")
        self.assertFalse(any(item.get("kind") == "audio" for item in result["outputs"]))
        manifest = jload(self.root / self.slug / "project.json")
        clip = next(item for item in manifest["clips"] if item["id"] == "clip_audio_fail_open")
        self.assertEqual(clip["status"], "delivered")
        self.assertFalse(any(item.get("kind") == "audio" for item in manifest["asset"]["media"]))

    def test_duplicate_rescan_preserves_user_selected_version(self) -> None:
        """A duplicate handoff must not reset the page's selected version."""
        video = self.task_dir / "selected-version.webm"
        video.write_bytes(b"V" * 4096)
        project = P(str(self.root), self.slug)
        clip_dir = Path(project.dir) / "clips" / "clip_selected"
        clip_dir.mkdir(parents=True, exist_ok=True)
        (clip_dir / "v1.webm").write_bytes(video.read_bytes())
        (clip_dir / "v2.webm").write_bytes(b"W" * 4096)
        clip = {
            "id": "clip_selected", "segment": None, "title": "Selected",
            "duration": None, "ratio": "9:16", "clarity": "Standard",
            "mappings": [], "preserve": None, "status": "delivered",
            "outcome": "complete", "current": 2,
            "versions": [
                {"v": 1, "file": "clips/clip_selected/v1.webm", "created": "2026-01-01T00:00:00Z"},
                {"v": 2, "file": "clips/clip_selected/v2.webm", "created": "2026-01-01T00:00:01Z"},
            ],
            "poster": None, "proxy": None, "filmstrip": None,
            "revision_note": None, "handoff_status": None,
            "handoff_message": None, "handoff_task_id": None,
            "handoff_updated": None,
        }
        document = jload(project.pj)
        document["status"] = "reviewing"
        document["asset"]["clips"] = [clip]
        document["clips"] = [clip]
        jwrite(project.pj, document)
        task_file = self.write_task("task_selected_version.json", {
            "task_id": "task_selected_version", "clip_id": "clip_selected",
            "status": "completed", "generation_status": "completed",
            "generation_completion_confirmed": True, "video_file": video.name,
        })

        with mock.patch.object(handoff_module, "derivatives", return_value={}), \
                mock.patch.object(handoff_module, "extract_audio_track", return_value=(None, "no audio")):
            result = handoff_module.handoff(self.root, self.slug, task_file, None)

        self.assertEqual(result["status"], "completed")
        saved = jload(project.pj)
        selected = next(item for item in saved["clips"] if item["id"] == "clip_selected")
        self.assertEqual(selected["current"], 2)
        self.assertEqual(len(selected["versions"]), 2)

    def test_generic_generator_handoff_is_received_and_idempotent(self) -> None:
        """The manager accepts a public result from any generator."""
        inbox = Path(self.temp.name) / "generic-inbox"
        inbox.mkdir(parents=True)
        video = inbox / "render.webm"
        video.write_bytes(b"\x00" * 2048)
        (inbox / "generic-result.json").write_text(json.dumps({
            "task_id": "generic_001",
            "generator": "example-video-skill",
            "status": "completed",
            "video_file": str(video),
            "project_title": "Generic result",
        }, ensure_ascii=False), encoding="utf-8")
        result = sync_module.scan(self.root, inbox, start_runtime=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["received"][0]["status"], "completed")
        slug = result["received"][0]["project"]
        self.assertEqual(slug, self.slug)
        manifest = jload(self.root / slug / "project.json")
        self.assertEqual(manifest["clips"][0]["versions"][0]["file"].endswith(".webm"), True)
        repeated = sync_module.scan(self.root, inbox, start_runtime=False)
        self.assertTrue(repeated["received"][0]["idempotent"])

    def test_capafy_virtual_output_path_uses_configured_local_root(self) -> None:
        inbox = Path(self.temp.name) / "agent-outputs"
        inbox.mkdir(parents=True)
        video = inbox / "generated_clip.mp4"
        video.write_bytes(b"\x00" * 2048)
        task = {
            "task_id": "task_capafy_virtual_fixture",
            "clip_id": "clip_capafy",
            "status": "failed",
            "generation_status": "completed",
            "generation_completion_confirmed": True,
            "delivery_status": "signing_failed",
            "failure_stage": "video_link_signing",
            # This is the path Capafy reports from its Linux worker.  The
            # actual bytes are in the explicitly configured local inbox.
            "video_file": "/home/user/outputs/generated_clip.mp4",
            "output_dir": str(inbox),
        }
        task_file = self.write_task("task_capafy_virtual_fixture.json", task)

        with mock.patch.object(handoff_module, "cache_remote_video") as cache_remote:
            with mock.patch.object(handoff_module, "derivatives", return_value={}):
                result = handoff_module.handoff(self.root, self.slug, task_file, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        cache_remote.assert_not_called()
        manifest = jload(self.root / self.slug / "project.json")
        clip = next(item for item in manifest["clips"] if item["id"] == "clip_capafy")
        self.assertEqual(clip["status"], "delivered")
        self.assertTrue((self.root / self.slug / clip["versions"][0]["file"]).is_file())

    def test_missing_capafy_virtual_path_is_partial_without_remote_guess(self) -> None:
        task = {
            "task_id": "task_capafy_missing_fixture",
            "clip_id": "clip_missing",
            "status": "failed",
            "generation_status": "completed",
            "generation_completion_confirmed": True,
            "delivery_status": "signing_failed",
            "failure_stage": "video_link_signing",
            "video_file": "/home/user/outputs/not-on-this-machine.mp4",
        }
        task_file = self.write_task("task_capafy_missing_fixture.json", task)

        with mock.patch.object(handoff_module, "cache_remote_video") as cache_remote:
            result = handoff_module.handoff(self.root, self.slug, task_file, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertIn("没有本地视频文件", result["message"])
        cache_remote.assert_not_called()
        self.assertEqual(jload(self.root / self.slug / "project.json")["clips"], [])

    def test_capafy_virtual_path_rejects_encoded_traversal(self) -> None:
        inbox = Path(self.temp.name) / "outputs"
        inbox.mkdir(parents=True)
        outside = Path(self.temp.name) / "escape.mp4"
        outside.write_bytes(b"\x00" * 2048)
        task = {
            "task_id": "task_capafy_traversal_fixture",
            "status": "complete",
            "generation_status": "completed",
            "generation_completion_confirmed": True,
            "video_file": "/home/user/outputs/%2e%2e/escape.mp4",
            "output_dir": str(inbox),
        }
        task_file = self.write_task("task_capafy_traversal_fixture.json", task)

        result = handoff_module.handoff(self.root, self.slug, task_file, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(jload(self.root / self.slug / "project.json")["clips"], [])

        task["task_id"] = "task_capafy_backslash_traversal_fixture"
        task["video_file"] = "/home/user/outputs/%5c..%5cescape.mp4"
        task_file = self.write_task("task_capafy_backslash_traversal_fixture.json", task)
        result = handoff_module.handoff(self.root, self.slug, task_file, None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")

        task["task_id"] = "task_capafy_encoded_path_fixture"
        task["video_file"] = "%2Fhome%2Fuser%2Foutputs%2Fescape.mp4"
        task_file = self.write_task("task_capafy_encoded_path_fixture.json", task)
        result = handoff_module.handoff(self.root, self.slug, task_file, None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "partial")

    def test_file_uri_local_path_is_accepted_in_approved_root(self) -> None:
        video = self.task_dir / "file-uri.mp4"
        video.write_bytes(b"\x00" * 2048)
        task = {
            "task_id": "task_file_uri_fixture",
            "clip_id": "clip_file_uri",
            "status": "complete",
            "generation_status": "completed",
            "generation_completion_confirmed": True,
            "video_file": video.as_uri(),
        }
        task_file = self.write_task("task_file_uri_fixture.json", task)

        with mock.patch.object(handoff_module, "derivatives", return_value={}):
            result = handoff_module.handoff(self.root, self.slug, task_file, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outputs"][0]["kind"], "clip")

    def test_small_local_report_file_is_not_rejected_by_video_size_guard(self) -> None:
        report = self.task_dir / "short-report.md"
        report.write_text("# Hook\n", encoding="utf-8")
        task = {
            "task_id": "task_short_report_fixture",
            "clip_id": "clip_short_report",
            "status": "complete",
            "generation_status": "completed",
            "generation_completion_confirmed": True,
            "report_file": report.name,
        }
        task_file = self.write_task("task_short_report_fixture.json", task)

        report_text, error = handoff_module.read_public_report(task, task_file)

        self.assertEqual(report_text, "# Hook")
        self.assertIsNone(error)

    def test_extensionless_local_video_with_container_signature_is_accepted(self) -> None:
        video = self.task_dir / "render-without-extension"
        video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"V" * 2048)
        task = {
            "task_id": "task_extensionless_fixture",
            "clip_id": "clip_extensionless",
            "status": "complete",
            "generation_status": "completed",
            "generation_completion_confirmed": True,
            "video_file": video.name,
        }
        task_file = self.write_task("task_extensionless_fixture.json", task)

        with mock.patch.object(handoff_module, "derivatives", return_value={}):
            result = handoff_module.handoff(self.root, self.slug, task_file, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["outputs"][0]["file"].endswith(".mp4"))

    def test_relative_object_key_in_video_url_stays_remote_reference(self) -> None:
        task = {
            "video_url": "videos/render-opaque-id",
        }
        resolved, error = handoff_module.local_video(task, None)
        self.assertIsNone(resolved)
        self.assertEqual(error, "视频结果未缓存到本地。")

    def test_unconfirmed_failure_keeps_failed_status_and_sanitized_reason(self) -> None:
        task = {
            "task_id": "task_generation_fixture",
            "status": "failed",
            "generation_status": "failed",
            "generation_completion_confirmed": False,
            "failure_stage": "generation",
            "failure_reason": "The video service rejected the request",
        }
        task_file = self.write_task("task_generation_fixture.json", task)

        result = handoff_module.handoff(self.root, self.slug, task_file, None)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["message"], "视频生成未完成。")
        self.assertNotIn(task["failure_reason"], json.dumps(self.read_summary("task_generation_fixture")))
        state = jload(self.root / self.slug / "state.json")
        self.assertEqual(state["last_error"]["message"], "视频生成未完成。")

    def test_failed_handoff_updates_existing_preflight_placeholder_once(self) -> None:
        project = P(str(self.root), self.slug)
        document = project.load()
        placeholder = {
            "id": "clip_reserved",
            "segment": None,
            "title": "Reserved clip",
            "duration": 4,
            "ratio": "9:16",
            "clarity": "Standard",
            "mappings": [],
            "preserve": None,
            "status": "generating",
            "outcome": None,
            "current": None,
            "versions": [],
            "poster": None,
            "proxy": None,
            "filmstrip": None,
            "revision_note": None,
        }
        document["clips"].append(placeholder)
        document["asset"]["clips"] = document["clips"]
        project.save(document, "project.prepared", {"clip": "clip_reserved"})
        task_file = self.write_task("task_reserved_failure.json", {
            "task_id": "task_reserved_failure",
            "clip_id": "clip_reserved",
            "status": "failed",
            "generation_status": "failed",
            "failure_stage": "generation",
            "failure_reason": "worker stopped",
        })

        first = handoff_module.handoff(self.root, self.slug, task_file, None)
        self.assertEqual(first["status"], "failed")
        manifest = jload(project.pj)
        clip = next(item for item in manifest["clips"] if item["id"] == "clip_reserved")
        self.assertEqual(clip["status"], "generating")
        self.assertEqual(clip["handoff_status"], "failed")
        self.assertEqual(clip["handoff_task_id"], "task_reserved_failure")
        self.assertEqual(clip["revision_note"], "worker stopped")
        revision = manifest["rev"]

        repeated = handoff_module.handoff(self.root, self.slug, task_file, None)
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(jload(project.pj)["rev"], revision)

    def test_late_failed_handoff_does_not_downgrade_delivered_clip(self) -> None:
        video = self.task_dir / "already-delivered.webm"
        video.write_bytes(b"V" * 2048)
        task_file = self.write_task("task_delivered_once.json", {
            "task_id": "task_delivered_once",
            "clip_id": "clip_late_failure",
            "status": "completed",
            "video_file": video.name,
        })
        with mock.patch.object(handoff_module, "derivatives", return_value={}):
            delivered = handoff_module.handoff(self.root, self.slug, task_file, None)
        self.assertEqual(delivered["status"], "completed")

        late_file = self.write_task("task_late_failure.json", {
            "task_id": "task_late_failure",
            "clip_id": "clip_late_failure",
            "status": "failed",
            "generation_status": "failed",
            "failure_stage": "generation",
            "failure_reason": "late worker error",
        })
        late = handoff_module.handoff(self.root, self.slug, late_file, None)
        self.assertEqual(late["status"], "failed")
        clip = next(item for item in jload(self.root / self.slug / "project.json")["clips"]
                    if item["id"] == "clip_late_failure")
        self.assertEqual(clip["status"], "delivered")
        self.assertEqual(len(clip["versions"]), 1)

    def test_sync_accepts_partial_terminal_task_without_generation_retry(self) -> None:
        task = {
            "task_id": "task_partial_fixture",
            "status": "partial",
            "generation_status": "completed",
            "generation_completion_confirmed": True,
            "delivery_status": "video_link_missing",
            "failure_stage": "video_link_missing",
        }
        task_file = self.write_task("task_partial_fixture.json", task)

        result = sync_module.scan(self.root, self.task_dir, start_runtime=False)

        self.assertTrue(result["ok"])
        received = next(item for item in result["received"] if item["task_id"] == "task_partial_fixture")
        self.assertEqual(received["status"], "partial")
        self.assertTrue((self.root / self.slug / "tasks/task_partial_fixture.json").is_file())
        # The source task is never rewritten or marked as a new generation;
        # the scan only records the sanitized handoff summary.
        self.assertEqual(json.loads(task_file.read_text(encoding="utf-8"))["status"], "partial")

    def test_sync_auto_creates_project_for_recoverable_failed_task(self) -> None:
        (self.root / "active_project").unlink()
        video = self.task_dir / "recoverable.webm"
        video.write_bytes(b"\x1a\x45\xdf\xa3" + b"V" * 2048)
        task = {
            "task_id": "task_recoverable_fixture",
            "status": "failed",
            "generation_status": "completed",
            "generation_completion_confirmed": True,
            "delivery_status": "signing_failed",
            "failure_stage": "video_link_signing",
            "video_file": video.name,
        }
        self.write_task("task_recoverable_fixture.json", task)

        with mock.patch.object(handoff_module, "derivatives", return_value={}):
            result = sync_module.scan(self.root, self.task_dir, start_runtime=False)

        received = next(item for item in result["received"] if item["task_id"] == "task_recoverable_fixture")
        self.assertEqual(received["status"], "completed")
        slug = sync_module._auto_project_slug("task_recoverable_fixture")
        manifest = jload(self.root / slug / "project.json")
        self.assertEqual(manifest["clips"][0]["status"], "delivered")
        self.assertTrue((self.root / slug / manifest["clips"][0]["versions"][0]["file"]).is_file())

    def test_sync_auto_creates_project_for_capafy_virtual_local_file(self) -> None:
        (self.root / "active_project").unlink()
        inbox = Path(self.temp.name) / "outputs"
        inbox.mkdir(parents=True)
        video = inbox / "capafy-sync.webm"
        video.write_bytes(b"\x1a\x45\xdf\xa3" + b"V" * 2048)
        task = {
            "task_id": "task_capafy_sync_fixture",
            "status": "complete",
            "generation_status": "completed",
            "generation_completion_confirmed": True,
            "delivery_status": "delivered",
            "video_file": "/home/user/agent-outputs/capafy-sync.webm",
            "output_dir": str(inbox),
        }
        self.write_task("task_capafy_sync_fixture.json", task)

        with mock.patch.object(handoff_module, "derivatives", return_value={}):
            result = sync_module.scan(self.root, self.task_dir, start_runtime=False)

        received = next(item for item in result["received"] if item["task_id"] == "task_capafy_sync_fixture")
        self.assertEqual(received["status"], "completed")
        slug = sync_module._auto_project_slug("task_capafy_sync_fixture")
        manifest = jload(self.root / slug / "project.json")
        self.assertEqual(manifest["clips"][0]["status"], "delivered")
        self.assertTrue((self.root / slug / manifest["clips"][0]["versions"][0]["file"]).is_file())

    def test_sync_auto_creates_project_and_caches_remote_webm(self) -> None:
        (self.root / "active_project").unlink()
        body = b"\x1a\x45\xdf\xa3" + b"R" * 4096

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                self.send_response(200)
                self.send_header("Content-Type", "video/webm")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            task = {
                "task_id": "task_remote_fixture",
                "status": "complete",
                "generation_status": "completed",
                "generation_completion_confirmed": True,
                "delivery_status": "remote_url_only",
                "video_url": f"http://127.0.0.1:{server.server_port}/fixture.webm",
                "report": "# Retention\n\nRemote fixture report.",
            }
            task_file = self.write_task("task_remote_fixture.json", task)
            with mock.patch.dict(os.environ, {"VAM_ALLOW_LOCAL_REMOTE": "1"}, clear=False), \
                    mock.patch.object(handoff_module, "derivatives", return_value={}):
                result = sync_module.scan(self.root, self.task_dir, start_runtime=False)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        received = next(item for item in result["received"] if item["task_id"] == "task_remote_fixture")
        self.assertEqual(received["status"], "completed")
        slug = sync_module._auto_project_slug("task_remote_fixture")
        manifest_path = self.root / slug / "project.json"
        manifest = jload(manifest_path)
        version_file = manifest["clips"][0]["versions"][0]["file"]
        self.assertTrue(version_file.endswith(".webm"))
        self.assertEqual((self.root / slug / version_file).read_bytes(), body)
        public_text = manifest_path.read_text(encoding="utf-8")
        public_text += (self.root / slug / "tasks/task_remote_fixture.json").read_text(encoding="utf-8")
        self.assertNotIn("http://127.0.0.1", public_text)
        # The generator task remains untouched; the manager only caches and records
        # a project-relative copy of the video.
        self.assertIn("video_url", task_file.read_text(encoding="utf-8"))

    def test_s3_reference_rejects_traversal_segments(self) -> None:
        self.assertTrue(handoff_module._remote_candidates("s3://bucket/path/result.webm"))
        self.assertEqual(handoff_module._remote_candidates("s3://bucket/../result.webm"), [])
        self.assertEqual(handoff_module._remote_candidates("s3://bucket/path/%00result.webm"), [])
        self.assertEqual(handoff_module._remote_candidates("https://user:pass@example.test/result.webm"), [])
        self.assertEqual(handoff_module._remote_candidates("https://example.test/a/%2e%2e/result.webm"), [])

    def test_nested_remote_result_shape_is_detected_without_reading_prompt(self) -> None:
        task = {
            "result": {"video": {"location": "s3://bucket/path/render-opaque-id"}},
            "prompt": "https://example.invalid/not-a-video.mp4",
        }
        self.assertEqual(
            handoff_module.remote_video_reference(task),
            "s3://bucket/path/render-opaque-id",
        )

    def test_cli_crash_is_reported_as_structured_json_error(self) -> None:
        """A handoff crash must emit one JSON result on stdout (never a bare
        traceback) and route the real diagnostics to stderr, so generator
        integrations never lose the failure outcome."""
        task_file = self.write_task("task_crash_fixture.json", {
            "task_id": "task_crash_fixture",
            "status": "completed",
        })
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(handoff_module, "handoff", side_effect=RuntimeError("boom")), \
             mock.patch.object(sys, "argv", ["handoff.py", "--root", str(self.root),
                                             "--project", self.slug, "--task-file", str(task_file)]):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = handoff_module.main()
        self.assertEqual(code, 1)
        lines = output.getvalue().strip().splitlines()
        payload = json.loads(lines[-1])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "视频交接失败。")
        self.assertNotIn("boom", output.getvalue())
        self.assertIn("Traceback", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
