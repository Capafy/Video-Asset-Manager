"""Regression tests for generator-neutral preflight preparation."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import vpm_prepare  # noqa: E402
import vpm_record  # noqa: E402
import vpm_sync  # noqa: E402


class PrepareFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="vam-prepare-test-")
        self.root = Path(self.temp.name) / "manager"
        self.input_dir = Path(self.temp.name) / "inputs"
        self.input_dir.mkdir()
        self.image = self.input_dir / "reference.png"
        self.image.write_bytes(b"PNG" + bytes(range(256)))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prepare_registers_public_input_asset_and_generating_clip(self) -> None:
        result = vpm_prepare.prepare({
            "title": "Product demo",
            "input_script": "0-4 seconds: a sample product rotates on a turntable.",
            "images": str(self.image),
            "task_id": "task-preflight-1",
            "duration_seconds": 4,
            "aspect_ratio": "9:16",
        }, self.root, open_window=False)

        self.assertTrue(result["ok"])
        slug = result["project_slug"]
        clip_id = result["clip_id"]
        project_dir = self.root / slug
        manifest = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "generating")
        self.assertEqual(manifest["asset"]["clips"][0]["id"], clip_id)
        self.assertEqual(manifest["asset"]["clips"][0]["status"], "generating")
        self.assertEqual(len(manifest["asset"]["scripts"]), 1)
        self.assertEqual(len(manifest["asset"]["media"]), 1)
        self.assertTrue((project_dir / manifest["asset"]["scripts"][0]["file"]).is_file())
        self.assertTrue((project_dir / manifest["asset"]["media"][0]["file"]).is_file())
        self.assertEqual((self.root / "active_project").read_text(encoding="utf-8").strip(), slug)
        mapping = json.loads((self.root / ".handoff_map.json").read_text(encoding="utf-8"))
        self.assertEqual(mapping["tasks"]["task-preflight-1"]["project_slug"], slug)
        self.assertEqual(mapping["tasks"]["task-preflight-1"]["clip_id"], clip_id)
        self.assertEqual(vpm_record.check_one(str(self.root), slug), [])

    def test_same_task_is_idempotent_and_reuses_project_and_clip(self) -> None:
        request = {
            "title": "Repeatable project",
            "script": "A short public script.",
            "task_id": "task-preflight-repeat",
        }
        first = vpm_prepare.prepare(request, self.root, open_window=False)
        second = vpm_prepare.prepare(request, self.root, open_window=False)
        self.assertEqual(first["project_slug"], second["project_slug"])
        self.assertEqual(first["clip_id"], second["clip_id"])
        projects = [path for path in self.root.iterdir() if path.is_dir() and (path / "project.json").is_file()]
        self.assertEqual(len(projects), 1)
        manifest = json.loads((projects[0] / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["clips"]), 1)
        self.assertEqual(len(manifest["asset"]["scripts"]), 1)

    def test_studio_caption_style_fields_pass_recorder_validation(self) -> None:
        document = vpm_record.empty_project("caption-style", "Caption style", "generate")
        document["assembly"]["timeline"] = {
            "schema": 2,
            "canvas": {"ratio": "9:16", "width": 1080, "height": 1920},
            "zoom": 1,
            "workspace_duration": 7,
            "snap": True,
            "tracks": [
                {"id": "video-main", "kind": "video", "clips": []},
                {"id": "video-overlay", "kind": "video", "clips": []},
                {"id": "video-2", "kind": "video", "clips": []},
                {"id": "video-3", "kind": "video", "clips": []},
                {"id": "audio-main", "kind": "audio", "clips": []},
                {"id": "text-main", "kind": "subtitle", "cues": [{
                    "id": "cue_studio", "start": 0, "end": 4, "text": "Hello",
                    "kind": "title", "style": {
                        "position": "top", "fontSize": 120,
                        "outlineColor": "#c92222", "outlineWidth": 4,
                        "backgroundOpacity": 0.4,
                    },
                }]},
            ],
        }
        self.assertEqual(vpm_record.timeline_issues(document), [])

    def test_runtime_failure_is_warning_only(self) -> None:
        with mock.patch.object(vpm_prepare, "open_runtime", return_value={"ok": False, "warning": "runtime unavailable"}):
            result = vpm_prepare.prepare({
                "title": "Runtime outage",
                "script": "The generator must continue.",
                "task_id": "task-preflight-runtime",
            }, self.root, open_window=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["manager_warning"], "runtime unavailable")
        self.assertTrue((self.root / result["project_slug"] / "project.json").is_file())

    def test_runtime_probe_has_a_short_fail_open_timeout(self) -> None:
        metadata = self.root / "webapp" / "runtime.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps({"root": str(self.root)}), encoding="utf-8")
        process = mock.Mock()
        process.poll.return_value = 0
        with mock.patch.object(vpm_prepare.subprocess, "Popen", return_value=process) as popen:
            result = vpm_prepare.open_runtime(self.root, "short-probe")
        self.assertTrue(result["ok"])
        popen.assert_called_once()
        self.assertLessEqual(vpm_prepare.RUNTIME_OPEN_OBSERVE_SECONDS, 2.0)
        self.assertGreater(vpm_prepare.RUNTIME_OPEN_OBSERVE_SECONDS, 0.0)

    def test_slow_runtime_launch_is_detached_and_fail_open(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        started = time.perf_counter()
        with mock.patch.object(vpm_prepare.subprocess, "Popen", return_value=process):
            result = vpm_prepare.open_runtime(self.root, "slow-probe")
        elapsed = time.perf_counter() - started
        self.assertTrue(result["ok"])
        self.assertTrue(result["pending"])
        self.assertLess(elapsed, 1.0)

    def test_runtime_launcher_avoids_libreoffice_python_shim(self) -> None:
        with mock.patch.object(vpm_prepare.sys, "executable",
                               r"C:\Program Files\LibreOffice\program\python-core-3.12.12"), \
             mock.patch.object(vpm_prepare.shutil, "which",
                               side_effect=lambda name: r"C:\Windows\py.exe" if name == "py" else None):
            self.assertEqual(vpm_prepare._python_command(), [r"C:\Windows\py.exe", "-3"])

    def test_runtime_failure_does_not_block_terminal_scan(self) -> None:
        task_dir = Path(self.temp.name) / "handoffs"
        task_dir.mkdir()
        video = task_dir / "result.webm"
        video.write_bytes(b"\x1a\x45\xdf\xa3" + b"V" * 4096)
        (task_dir / "task-preflight-scan.json").write_text(json.dumps({
            "task_id": "task-preflight-scan",
            "status": "completed",
            "video_file": video.name,
        }), encoding="utf-8")
        with mock.patch.object(vpm_sync, "ensure_runtime", return_value={
            "ok": False, "error": "port conflict",
        }), mock.patch.object(vpm_sync, "derivatives", create=True):
            # The adapter's derivative helper lives in its imported module;
            # avoid ffmpeg work while exercising the scanner's fail-open path.
            with mock.patch("handoff_impl.derivatives", return_value={}):
                result = vpm_sync.scan(self.root, task_dir, start_runtime=True)
        self.assertTrue(result["ok"])
        self.assertIn("manager_warning", result)
        self.assertEqual(len(result["received"]), 1)
        self.assertEqual(result["received"][0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
