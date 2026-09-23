"""Tests for the generator-neutral completion receiver."""
from __future__ import annotations

import json
import contextlib
import io
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import vpm_receive  # noqa: E402
import receive  # noqa: E402
import vpm_sync  # noqa: E402


class ReceiveFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="vam-receive-test-")
        self.root = Path(self.temp.name) / "manager"
        self.output = Path(self.temp.name) / "outputs"
        self.output.mkdir(parents=True)
        # The handoff adapter intentionally rejects implausibly tiny media;
        # this fixture is large enough to exercise the normal path without
        # pretending to be a playable container.
        self.video = self.output / "render.webm"
        self.video.write_bytes(bytes(range(256)) * 8)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_resolve_output_dirs_accepts_path_iterables(self) -> None:
        first = Path(self.temp.name) / "outputs-a"
        second = Path(self.temp.name) / "outputs-b"
        first.mkdir()
        second.mkdir()
        resolved = vpm_sync.resolve_output_dirs([first, second], manager_root=self.root)
        self.assertEqual({item.resolve() for item in resolved}, {first.resolve(), second.resolve()})

    def test_ensure_sync_receives_bare_output_without_task_dir(self) -> None:
        # The startup path must import a standalone generator file before it
        # returns, even when no JSON handoff inbox exists yet.
        ensure_path = SCRIPTS.parent / "assets" / "webapp" / "ensure.py"
        spec = importlib.util.spec_from_file_location("vam_ensure_test", ensure_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        missing_tasks = Path(self.temp.name) / "not-created-yet"
        with mock.patch.dict(os.environ, {"VIDEO_GENERATOR_OUTPUT_DIR": str(self.output)}, clear=False):
            result = module._sync_with_vpm_sync(
                self.root, None, str(missing_tasks), None, 25, None,
            )
        self.assertIsNotNone(result)
        assert isinstance(result, dict)
        self.assertTrue(result.get("ok"))
        self.assertEqual(result.get("imported"), 1)
        self.assertEqual(len(result.get("results", [])), 1)
        self.assertTrue(list(self.root.glob("video-*/project.json")))

    def test_installed_ensure_upgrade_uses_bundled_modules(self) -> None:
        """An installed launcher can upgrade another manager root offline."""
        ensure_path = SCRIPTS.parent / "assets" / "webapp" / "ensure.py"
        spec = importlib.util.spec_from_file_location("vam_ensure_bundle_test", ensure_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        installed = Path(self.temp.name) / "installed" / "webapp"
        target = Path(self.temp.name) / "target" / "webapp"
        installed.mkdir(parents=True)
        (installed / "vpm_sync.py").write_text("bundled-sync\n", encoding="utf-8")
        # Simulate an installed copy whose original skill/scripts directory is
        # gone.  The upgrade target is a separate manager root.
        module.HERE = installed
        module._install_bundle(target, upgrade=True)
        self.assertEqual(
            (target / "vpm_sync.py").read_text(encoding="utf-8"),
            "bundled-sync\n",
        )

    def test_local_file_auto_project_and_idempotent_repeat(self) -> None:
        first = vpm_receive.receive(
            self.video,
            root=self.root,
            start_runtime=False,
        )
        self.assertTrue(first["ok"])
        self.assertEqual(len(first["received"]), 1)
        self.assertFalse(first["received"][0]["idempotent"])

        second = vpm_receive.receive(
            self.video,
            root=self.root,
            start_runtime=False,
        )
        self.assertTrue(second["ok"])
        self.assertEqual(len(second["received"]), 1)
        self.assertTrue(second["received"][0]["idempotent"])

    def test_output_dir_resolution_accepts_path_iterables_and_rejects_mappings(self) -> None:
        first = Path(self.temp.name) / "generator-outputs-a"
        second = Path(self.temp.name) / "generator-outputs-b"
        first.mkdir()
        second.mkdir()

        # The resolver is a public Python API as well as the CLI's env parser;
        # Path objects and iterable path collections must remain individual
        # directories instead of being stringified into one bogus path.
        resolved = vpm_sync.resolve_output_dirs(
            (item for item in (first, second)), manager_root=self.root,
        )
        self.assertEqual(resolved, [first.resolve(), second.resolve()])
        self.assertEqual(
            vpm_sync.resolve_output_dirs([str(first), str(second)], manager_root=self.root),
            [first.resolve(), second.resolve()],
        )
        self.assertEqual(
            vpm_sync.resolve_output_dirs(os.fsencode(str(first)), manager_root=self.root),
            [first.resolve()],
        )
        self.assertEqual(vpm_sync.resolve_output_dirs({"path": str(first)}, manager_root=self.root), [])
        self.assertEqual(vpm_sync.resolve_output_dirs(42, manager_root=self.root), [])

    def test_output_dir_resolution_rejects_manager_root_and_broad_ancestors(self) -> None:
        allowed = Path(self.temp.name) / "allowed-outputs"
        allowed.mkdir()
        # Scanning the manager root or its containing workspace would expose
        # private state and turn a typo into a broad filesystem crawl.
        resolved = vpm_sync.resolve_output_dirs(
            [self.root, Path(self.temp.name), allowed], manager_root=self.root,
        )
        self.assertEqual(resolved, [allowed.resolve()])

    def test_scan_include_outputs_receives_bare_video_and_skips_repeat(self) -> None:
        output_dir = Path(self.temp.name) / "bare-output"
        output_dir.mkdir()
        bare_video = output_dir / "generator-result.mov"
        bare_video.write_bytes(self.video.read_bytes())
        task_dir = Path(self.temp.name) / "empty-handoffs"
        task_dir.mkdir()

        first = vpm_sync.scan(
            self.root,
            task_dir,
            start_runtime=False,
            include_outputs=True,
            # Exercise the generator/iterable API path, not just the CLI list.
            output_dirs=(item for item in (output_dir,)),
        )
        self.assertTrue(first["ok"])
        self.assertEqual(first.get("imported"), 1)
        self.assertEqual(len(first["received"]), 1)
        slug = first["received"][0]["project"]
        manifest_path = self.root / slug / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["asset"]["clips"]), 1)
        self.assertTrue((self.root / slug / manifest["asset"]["clips"][0]["versions"][0]["file"]).is_file())

        second = vpm_sync.scan(
            self.root,
            task_dir,
            start_runtime=False,
            include_outputs=True,
            output_dirs=[output_dir],
        )
        self.assertTrue(second["ok"])
        self.assertEqual(second.get("imported"), 0)
        self.assertEqual(second["received"], [])
        manifest_again = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest_again["asset"]["clips"]), 1)

    def test_bare_output_does_not_absorb_stale_active_project(self) -> None:
        # Establish an unrelated project and leave it selected in the UI.
        stale = vpm_receive.receive(self.video, root=self.root, start_runtime=False)
        stale_slug = stale["received"][0]["project"]
        output_dir = Path(self.temp.name) / "new-generator-output"
        output_dir.mkdir()
        new_video = output_dir / "new-result.webm"
        new_video.write_bytes(bytes(reversed(range(256))) * 8)
        task_dir = Path(self.temp.name) / "empty-handoffs-2"
        task_dir.mkdir()

        result = vpm_sync.scan(
            self.root,
            task_dir,
            start_runtime=False,
            include_outputs=True,
            output_dirs=[output_dir],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["received"]), 1)
        self.assertNotEqual(result["received"][0]["project"], stale_slug)
        self.assertEqual(
            (self.root / "active_project").read_text(encoding="utf-8").strip(),
            result["received"][0]["project"],
        )

    def test_cross_directory_task_reference_does_not_duplicate_bare_video(self) -> None:
        """A task inbox JSON and a separate output bucket may point to one file."""
        task_dir = Path(self.temp.name) / "task-inbox"
        output_dir = Path(self.temp.name) / "generator-output"
        task_dir.mkdir()
        output_dir.mkdir()
        video = output_dir / "render.webm"
        video.write_bytes(self.video.read_bytes())
        # Use the common Capafy virtual path; output_dir lets the normal
        # handoff adapter resolve it to the local copy.
        (task_dir / "result.json").write_text(json.dumps({
            "task_id": "cross-directory-1",
            "status": "completed",
            "video_file": "/home/user/outputs/render.webm",
            "output_dir": str(output_dir),
        }), encoding="utf-8")

        result = vpm_sync.scan(
            self.root,
            task_dir,
            start_runtime=False,
            include_outputs=True,
            output_dirs=[output_dir],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["received"]), 1)
        self.assertEqual(result["received"][0]["task_id"], "cross-directory-1")
        # ``imported`` counts only the fallback receiver's imports; the
        # ordinary task scan reports its delivery through ``received``.
        self.assertEqual(result.get("imported"), 0)
        self.assertTrue(any(
            isinstance(item, dict)
            and item.get("reason") == "output_referenced_by_task"
            for item in result.get("skipped", [])
        ))
        slug = result["received"][0]["project"]
        manifest = json.loads((self.root / slug / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["asset"]["clips"]), 1)

        repeated = vpm_sync.scan(
            self.root,
            task_dir,
            start_runtime=False,
            include_outputs=True,
            output_dirs=[output_dir],
        )
        self.assertTrue(repeated["ok"])
        self.assertEqual(len(repeated["received"]), 1)
        self.assertTrue(repeated["received"][0]["idempotent"])
        manifest_again = json.loads((self.root / slug / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest_again["asset"]["clips"]), 1)

    def test_cross_directory_same_content_copy_is_skipped_by_digest(self) -> None:
        """A copied artifact with another path must not become a second clip."""
        task_dir = Path(self.temp.name) / "task-inbox-content"
        output_dir = Path(self.temp.name) / "generator-output-content"
        task_dir.mkdir()
        output_dir.mkdir()
        primary = task_dir / "primary.webm"
        duplicate = output_dir / "renamed-copy.webm"
        primary.write_bytes(self.video.read_bytes())
        duplicate.write_bytes(self.video.read_bytes())
        (task_dir / "result.json").write_text(json.dumps({
            "task_id": "cross-content-1",
            "status": "completed",
            "video_file": primary.name,
        }), encoding="utf-8")

        result = vpm_sync.scan(
            self.root,
            task_dir,
            start_runtime=False,
            include_outputs=True,
            output_dirs=[output_dir],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["received"]), 1)
        self.assertTrue(any(
            isinstance(item, dict)
            and item.get("reason") == "output_matches_task"
            for item in result.get("skipped", [])
        ))
        slug = result["received"][0]["project"]
        manifest = json.loads((self.root / slug / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["asset"]["clips"]), 1)

    def test_same_directory_unresolved_virtual_path_does_not_hide_bare_video(self) -> None:
        """An invalid worker reference must not suppress a usable local file."""
        task_dir = Path(self.temp.name) / "same-directory-invalid-ref"
        task_dir.mkdir()
        video = task_dir / "render.webm"
        video.write_bytes(self.video.read_bytes())
        # Encoded traversal is rejected by the approved-path resolver.  The
        # colocated bare file is nevertheless a valid generator result and
        # should be picked up by the bounded fallback watcher.
        (task_dir / "result.json").write_text(json.dumps({
            "task_id": "same-directory-invalid-ref-1",
            "status": "completed",
            "video_file": "/home/user/outputs/%2e%2e/render.webm",
        }), encoding="utf-8")

        result = vpm_sync.scan(
            self.root,
            task_dir,
            start_runtime=False,
            include_outputs=True,
            output_dirs=[task_dir],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["received"]), 1)
        self.assertEqual(result["received"][0]["status"], "completed")
        self.assertEqual(result.get("imported"), 1)

    def test_inline_input_script_is_registered_and_reported(self) -> None:
        script = (
            "# 4秒脚本：产品演示\n\n"
            "- 0–1秒：产品进入画面，灯光亮起。\n"
            "- 1–2秒：转台旋转展示侧面。"
        )
        first = vpm_receive.receive(
            self.video,
            root=self.root,
            start_runtime=False,
            task_id="script-inline-1",
            title="脚本交接测试",
            script=script,
            script_name="首次视频生成输入",
        )
        self.assertTrue(first["ok"])
        row = first["received"][0]
        self.assertEqual(row.get("script"), "assets/scripts/input-" +
                         row["outputs"][1]["sha256"][7:31] + ".md")
        slug = row["project"]
        manifest = json.loads((self.root / slug / "project.json").read_text(encoding="utf-8"))
        scripts = manifest["asset"]["scripts"]
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts[0]["name"], "首次视频生成输入")
        script_path = self.root / slug / scripts[0]["file"]
        self.assertEqual(script_path.read_text(encoding="utf-8").strip(), script.strip())

        repeated = vpm_receive.receive(
            self.video,
            root=self.root,
            start_runtime=False,
            task_id="script-inline-1",
            script=script,
        )
        self.assertTrue(repeated["ok"])
        self.assertTrue(repeated["received"][0]["idempotent"])
        manifest_again = json.loads((self.root / slug / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest_again["asset"]["scripts"]), 1)

    def test_result_json_input_script_syncs_and_repeat_scan_is_idempotent(self) -> None:
        # Keep the video beside the result JSON so the normal approved-root
        # rule applies, just as it does for a generator's handoff directory.
        task_dir = Path(self.temp.name) / "json-handoffs"
        task_dir.mkdir()
        video = task_dir / "generated.webm"
        video.write_bytes(self.video.read_bytes())
        task_file = task_dir / "result-with-input.json"
        task_file.write_text(json.dumps({
            "task_id": "json-input-script-1",
            "status": "completed",
            "generator": "generic-video-skill",
            "video_file": video.name,
            "input_script": "第一镜：产品进入画面。\n第二镜：镜头展示细节。",
            "input_script_name": "首次视频生成输入",
            # This must never cross the public handoff boundary.
            "prompt": "private transformed prompt",
        }, ensure_ascii=False), encoding="utf-8")

        first = vpm_sync.scan(self.root, task_dir, start_runtime=False)
        self.assertTrue(first["ok"])
        self.assertEqual(len(first["received"]), 1)
        received = first["received"][0]
        self.assertFalse(received["idempotent"])
        self.assertEqual(received.get("script"), received["outputs"][1]["file"])
        slug = received["project"]
        manifest = json.loads((self.root / slug / "project.json").read_text(encoding="utf-8"))
        scripts = manifest["asset"]["scripts"]
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts[0]["name"], "首次视频生成输入")
        script_path = self.root / slug / scripts[0]["file"]
        self.assertIn("第一镜", script_path.read_text(encoding="utf-8"))
        self.assertNotIn("private transformed prompt", script_path.read_text(encoding="utf-8"))

        second = vpm_sync.scan(self.root, task_dir, start_runtime=False)
        self.assertTrue(second["ok"])
        self.assertEqual(len(second["received"]), 1)
        self.assertTrue(second["received"][0]["idempotent"])
        manifest_again = json.loads((self.root / slug / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest_again["asset"]["scripts"]), 1)

    def test_runner_result_nested_task_content_download_is_received(self) -> None:
        """Accept a generator's nested ``task/content`` envelope."""
        task_dir = Path(self.temp.name) / "nested-runner"
        task_dir.mkdir()
        video = task_dir / "generated.webm"
        video.write_bytes(self.video.read_bytes())
        result_file = task_dir / "runner-result.json"
        result_file.write_text(json.dumps({
            "status": "succeeded",
            "delivery_ready": True,
            "task": {
                "status": "succeeded",
                "content": {
                    "downloaded_files": [video.name],
                },
            },
            "input_script": "用户第一次提供的原创视频输入。",
        }, ensure_ascii=False), encoding="utf-8")

        result = vpm_receive.receive(result_file, root=self.root, start_runtime=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["received"][0]["status"], "completed")
        slug = result["received"][0]["project"]
        manifest = json.loads((self.root / slug / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["asset"]["clips"]), 1)
        self.assertEqual(len(manifest["asset"]["scripts"]), 1)

    def test_duplicate_envelopes_same_task_id_choose_richer_newest_once(self) -> None:
        """A legacy video-only envelope must not erase the public input script."""
        task_dir = Path(self.temp.name) / "duplicate-handoffs"
        task_dir.mkdir()
        video = task_dir / "generated.webm"
        video.write_bytes(self.video.read_bytes())
        legacy = task_dir / "legacy.json"
        legacy.write_text(json.dumps({
            "task_id": "duplicate-envelope-1",
            "status": "completed",
            "video_file": video.name,
        }, ensure_ascii=False), encoding="utf-8")
        current = task_dir / "current.json"
        current.write_text(json.dumps({
            "task_id": "duplicate-envelope-1",
            "status": "completed",
            "video_file": video.name,
            "input_script": "第一镜：产品进入画面。\n第二镜：镜头展示细节。",
        }, ensure_ascii=False), encoding="utf-8")
        # Make the legacy envelope newer on purpose.  Public-field richness,
        # rather than mtime alone, must preserve the original input script.
        current_stat = current.stat()
        os.utime(legacy, (current_stat.st_atime, current_stat.st_mtime + 2.0))

        first = vpm_sync.scan(self.root, task_dir, start_runtime=False)
        self.assertTrue(first["ok"])
        self.assertEqual(first["scanned"], 2)
        self.assertEqual(len(first["received"]), 1)
        self.assertFalse(first["received"][0]["idempotent"])
        self.assertIsNotNone(first["received"][0].get("script"))
        slug = first["received"][0]["project"]
        manifest_path = self.root / slug / "project.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["asset"]["scripts"]), 1)
        rev = manifest["rev"]
        timeline = self.root / slug / "logs" / "timeline.jsonl"
        timeline_count = len(timeline.read_text(encoding="utf-8").splitlines())

        second = vpm_sync.scan(self.root, task_dir, start_runtime=False)
        self.assertTrue(second["ok"])
        self.assertEqual(len(second["received"]), 1)
        self.assertTrue(second["received"][0]["idempotent"])
        manifest_again = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest_again["rev"], rev)
        self.assertEqual(len(manifest_again["asset"]["scripts"]), 1)
        self.assertEqual(len(timeline.read_text(encoding="utf-8").splitlines()), timeline_count)

    def test_receive_cli_forwards_result_json_input_script(self) -> None:
        task_dir = Path(self.temp.name) / "cli-handoffs"
        task_dir.mkdir()
        destination = Path(self.temp.name) / "cli-inbox"
        destination.mkdir()
        video = task_dir / "generated.webm"
        video.write_bytes(self.video.read_bytes())
        task_file = task_dir / "result.json"
        task_file.write_text(json.dumps({
            "task_id": "cli-json-input-1",
            "status": "completed",
            "video_file": video.name,
            "input_script": "用户第一次提供的做视频输入。",
        }, ensure_ascii=False), encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = receive.main([
                "--result", str(task_file), "--root", str(self.root),
                "--task-dir", str(destination), "--no-open",
            ])
        self.assertEqual(code, 0, output.getvalue())
        response = json.loads(output.getvalue().strip().splitlines()[-1])
        self.assertEqual(response.get("status"), "completed")
        self.assertTrue(response.get("script", "").startswith("assets/scripts/input-"))
        slug = response["project"]
        manifest = json.loads((self.root / slug / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["asset"]["scripts"]), 1)
        script_file = self.root / slug / manifest["asset"]["scripts"][0]["file"]
        self.assertIn("用户第一次提供的做视频输入", script_file.read_text(encoding="utf-8"))

    def test_script_file_and_public_aliases_are_supported(self) -> None:
        script_file = self.output / "input.md"
        script_file.write_text("第一镜：产品从画面底部出现。\n", encoding="utf-8")
        result = vpm_receive.receive(
            self.video,
            root=self.root,
            start_runtime=False,
            task_id="script-file-1",
            script_file=str(script_file),
            script_name="原始 brief",
        )
        self.assertTrue(result["ok"])
        row = result["received"][0]
        slug = row["project"]
        manifest = json.loads((self.root / slug / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["asset"]["scripts"]), 1)
        registered = manifest["asset"]["scripts"][0]
        self.assertTrue(registered["file"].startswith("assets/scripts/input-"))
        self.assertEqual((self.root / slug / registered["file"]).read_text(encoding="utf-8").strip(), script_file.read_text(encoding="utf-8").strip())

    def test_private_prompt_fields_are_not_used_as_input_script(self) -> None:
        task_file = self.output / "private-prompt.json"
        task_file.write_text(json.dumps({
            "task_id": "private-prompt-1",
            "status": "completed",
            "video_file": str(self.video),
            "prompt": "内部转换 prompt，不应显示",
            "original_prompt": "也不应显示",
            "user_prompt": "也不应显示",
        }, ensure_ascii=False), encoding="utf-8")
        result = vpm_receive.receive(task_file, root=self.root, start_runtime=False)
        self.assertTrue(result["ok"])
        slug = result["received"][0]["project"]
        manifest = json.loads((self.root / slug / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["asset"]["scripts"], [])
        public_files = [p for p in (self.root / slug).rglob("*") if p.is_file()]
        for path in public_files:
            if path.suffix.lower() in {".json", ".md", ".txt", ".log"}:
                text = path.read_text(encoding="utf-8", errors="ignore")
                self.assertNotIn("内部转换", text)
                self.assertNotIn("original_prompt", text)

    def test_script_sanitization_drops_secret_lines_but_keeps_public_lines(self) -> None:
        script = (
            "公开镜头：产品旋转。\n"
            "api_key=do-not-persist\n"
            "下载地址：https://example.invalid/private.mp4\n"
            "本地路径：C:\\Users\\example\\secret.mp4\n"
            "公开结尾：字幕出现。"
        )
        result = vpm_receive.receive(
            self.video,
            root=self.root,
            start_runtime=False,
            task_id="script-sanitize-1",
            script=script,
        )
        self.assertTrue(result["ok"])
        slug = result["received"][0]["project"]
        manifest = json.loads((self.root / slug / "project.json").read_text(encoding="utf-8"))
        registered = manifest["asset"]["scripts"][0]
        text = (self.root / slug / registered["file"]).read_text(encoding="utf-8")
        self.assertIn("公开镜头", text)
        self.assertIn("公开结尾", text)
        self.assertNotIn("do-not-persist", text)
        self.assertNotIn("example.invalid", text)
        self.assertNotIn("C:\\Users", text)

    def test_json_private_fields_are_not_persisted(self) -> None:
        task_file = self.output / "result.json"
        task_file.write_text(
            json.dumps(
                {
                    "task_id": "generic-private-fixture",
                    "status": "completed",
                    "generator": "generic-test",
                    "video_file": str(self.video),
                    "provider": "secret-provider",
                    "api_key": "should-not-persist",
                    "message": "公开结果",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result = vpm_receive.receive(task_file, root=self.root, start_runtime=False)
        self.assertTrue(result["ok"])
        slug = result["received"][0]["project"]
        project_root = self.root / slug
        for path in project_root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".json", ".md", ".txt", ".log"}:
                text = path.read_text(encoding="utf-8", errors="ignore")
                self.assertNotIn("should-not-persist", text)
                self.assertNotIn("secret-provider", text)
                self.assertNotIn("api_key", text.lower())

    def test_watch_once_no_start_does_not_forward_cli_flag(self) -> None:
        result_dir = Path(self.temp.name) / "watch"
        result_dir.mkdir()
        watch_video = result_dir / "watch.webm"
        watch_video.write_bytes(self.video.read_bytes())
        status = vpm_receive.watch(
            result_dir,
            once=True,
            no_start=True,
            root=self.root,
        )
        self.assertEqual(status, 0)

    def test_direct_media_digest_keeps_different_files_distinct(self) -> None:
        first_video = self.output / "first.mp4"
        second_video = self.output / "second.webm"
        first_video.write_bytes(bytes(range(256)) * 8)
        second_video.write_bytes(bytes(reversed(range(256))) * 8)
        first = vpm_receive.receive(first_video, root=self.root, start_runtime=False)
        second = vpm_receive.receive(second_video, root=self.root, start_runtime=False)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertNotEqual(first["received"][0]["task_id"], second["received"][0]["task_id"])

    def test_unsafe_task_id_does_not_fall_back_to_filename(self) -> None:
        first_file = self.output / "unsafe-a.webm"
        second_file = self.output / "unsafe-b.webm"
        first_file.write_bytes(bytes(range(256)) * 8)
        second_file.write_bytes(bytes(reversed(range(256))) * 8)
        first_task = self.output / "unsafe-a.json"
        second_task = self.output / "unsafe-b.json"
        first_task.write_text(json.dumps({
            "task_id": "C:\\outside\\secret",
            "status": "completed",
            "video_file": str(first_file),
        }), encoding="utf-8")
        second_task.write_text(json.dumps({
            "task_id": "C:\\outside\\secret",
            "status": "completed",
            "video_file": str(second_file),
        }), encoding="utf-8")
        first = vpm_receive.receive(first_task, root=self.root, start_runtime=False)
        second = vpm_receive.receive(second_task, root=self.root, start_runtime=False)
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertNotEqual(first["received"][0]["task_id"], second["received"][0]["task_id"])
        self.assertNotIn("outside", first["received"][0]["task_id"])

    def test_cli_aliases_and_no_open_stage_metadata(self) -> None:
        task_file = self.output / "alias-result.json"
        task_file.write_text(
            json.dumps({"status": "completed", "video_file": str(self.video)}, ensure_ascii=False),
            encoding="utf-8",
        )
        command = [
            "--result-file", str(task_file),
            "--task-id", "alias-task-1",
            "--generator", "alias-generator",
            "--title", "Alias project",
            "--message", "公开完成",
            "--root", str(self.root),
            "--no-open",
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            return_code = receive.main(command)
        self.assertEqual(return_code, 0, output.getvalue())
        inbox = self.root / "inbox"
        staged = json.loads((inbox / "alias-task-1.json").read_text(encoding="utf-8"))
        self.assertEqual(staged["task_id"], "alias-task-1")
        self.assertEqual(staged["generator"], "alias-generator")
        self.assertIn("video_file", staged)
        self.assertTrue(staged["video_file"].startswith("alias-task-1-"))

    def test_bare_video_cli_id_is_content_stable(self) -> None:
        first = self.output / "same-name-a.webm"
        second = self.output / "same-name-b.webm"
        first.write_bytes(bytes(range(256)) * 8)
        second.write_bytes(bytes(range(255, -1, -1)) * 8)

        def invoke(path: Path) -> dict:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = receive.main([
                    "--video-file", str(path), "--root", str(self.root), "--no-open",
                ])
            self.assertEqual(code, 0, output.getvalue())
            return json.loads(output.getvalue().strip().splitlines()[-1])

        first_result = invoke(first)
        second_result = invoke(second)
        self.assertNotEqual(first_result["task_id"], second_result["task_id"])

        repeat_result = invoke(first)
        self.assertEqual(first_result["task_id"], repeat_result["task_id"])
        self.assertTrue(repeat_result.get("idempotent"))

    def test_inline_json_and_no_sync_leave_atomic_inbox_record(self) -> None:
        payload = json.dumps({
            "task_id": "inline-json-1",
            "status": "completed",
            "generator": "inline-generator",
            "message": "inline message",
        }, ensure_ascii=False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = receive.main([
                "--json", payload,
                "--video-file", str(self.video),
                "--root", str(self.root),
                "--no-sync", "--no-open",
            ])
        self.assertEqual(code, 0, output.getvalue())
        record = json.loads((self.root / "inbox" / "inline-json-1.json").read_text(encoding="utf-8"))
        self.assertEqual(record["task_id"], "inline-json-1")
        self.assertEqual(record["message"], "inline message")
        self.assertEqual(record["generator"], "inline-generator")
        self.assertEqual(json.loads(output.getvalue())["scanned"], 0)

    def test_video_url_alias_is_kept_only_in_inbox_handoff(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = receive.main([
                "--video-url", "https://example.com/render.webm",
                "--task-id", "url-alias-1",
                "--root", str(self.root),
                "--no-sync", "--no-open",
            ])
        self.assertEqual(code, 0, output.getvalue())
        record = json.loads((self.root / "inbox" / "url-alias-1.json").read_text(encoding="utf-8"))
        self.assertEqual(record["video_url"], "https://example.com/render.webm")

    def test_no_sync_starts_runtime_but_never_scans(self) -> None:
        with mock.patch.object(receive, "ensure_runtime", return_value={"ok": True, "started_now": True}) as ensure:
            with mock.patch.object(receive, "scan") as scan:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = receive.main([
                        "--video-file", str(self.video), "--root", str(self.root),
                        "--no-sync",
                    ])
        self.assertEqual(code, 0, output.getvalue())
        ensure.assert_called_once()
        scan.assert_not_called()

    def test_no_open_scans_without_starting_runtime(self) -> None:
        fake_scan = {
            "ok": True,
            "scanned": 1,
            "eligible": 1,
            "received": [{"task_id": "no-open-1", "project": "p", "clip_id": "c",
                           "status": "completed", "idempotent": False, "outputs": []}],
            "pending": [],
            "pending_count": 0,
        }
        with mock.patch.object(receive, "ensure_runtime") as ensure:
            with mock.patch.object(receive, "scan", return_value=fake_scan) as scan:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = receive.main([
                        "--video-file", str(self.video), "--root", str(self.root),
                        "--no-open",
                    ])
        self.assertEqual(code, 0, output.getvalue())
        ensure.assert_not_called()
        self.assertFalse(scan.call_args.kwargs["start_runtime"])

    def test_receive_summaries_preserve_preview_path_without_runtime_details(self) -> None:
        """A terminal handoff can tell the host where to focus Preview."""
        raw = {
            "ok": True,
            "runtime": {
                "ok": True,
                "started_now": True,
                "mode": "capafy-preview",
                "pid": 1234,
                "preview": {
                    "status": "ready",
                    "version": 7,
                    "path": "/instance/abc-123/",
                    "url": "https://private.example/should-not-pass",
                },
            },
            "received": [],
        }
        compact = vpm_receive._compact_scan(raw)
        self.assertEqual(compact["runtime"]["preview"], {
            "status": "ready", "version": 7, "path": "/instance/abc-123/",
        })
        self.assertNotIn("pid", compact["runtime"])

        summary = receive._result_summary(raw, Path("handoff.json"))
        self.assertEqual(summary["runtime"]["preview"]["path"], "/instance/abc-123/")
        self.assertNotIn("pid", summary["runtime"])

    def test_receive_summaries_reject_non_instance_preview_path(self) -> None:
        raw = {
            "ok": True,
            "runtime": {
                "ok": True,
                "preview": {"status": "ready", "path": "https://example.invalid/"},
            },
        }
        compact = vpm_receive._compact_scan(raw)
        self.assertNotIn("preview", compact["runtime"])
        summary = receive._result_summary(raw, Path("handoff.json"))
        self.assertNotIn("preview", summary["runtime"])

    def test_managed_http_runtime_never_emits_legacy_preview(self) -> None:
        raw = {
            "ok": True,
            "runtime": {
                "ok": True,
                "started_now": True,
                "mode": "managed-http",
                "port": 4200,
                "health": "/api/health",
                "entry": "/",
                "window": {
                    "open": True,
                    "action": "open_or_focus",
                    "port": 4200,
                    "route": "#/p/demo/overview",
                },
                "preview": {"path": "/instance/should-not-pass/"},
            },
            "received": [],
        }
        compact = vpm_receive._compact_scan(raw)
        self.assertEqual(compact["runtime"]["mode"], "managed-http")
        self.assertEqual(compact["runtime"]["window"]["port"], 4200)
        self.assertNotIn("preview", compact["runtime"])
        summary = receive._result_summary(raw, Path("handoff.json"))
        self.assertEqual(summary["runtime"]["mode"], "managed-http")
        self.assertNotIn("preview", summary["runtime"])


class WorkspaceLibraryUsageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = SCRIPTS.parent / "assets" / "webapp" / "server.py"
        spec = importlib.util.spec_from_file_location("vam_workspace_usage_test", path)
        assert spec is not None and spec.loader is not None
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)

    def library(self, documents: list[dict]) -> dict:
        before = json.dumps(documents, sort_keys=True)
        lookup = {document["slug"]: document for document in documents}
        index = {"projects": [{"slug": slug} for slug in lookup]}

        def entries(document: dict) -> list[dict]:
            result = []
            for collection, prefix in (("media", ""), ("clips", "clip:")):
                for record in document["asset"].get(collection, []):
                    if record.get("status") == "trashed":
                        continue
                    result.append({"project_slug": document["slug"], "id": prefix + record["id"]})
            return result

        with mock.patch.object(self.server, "read_index_cached", return_value=index), \
             mock.patch.object(self.server, "workspace_project", side_effect=lambda slug: lookup[slug]), \
             mock.patch.object(self.server, "workspace_library_entries", side_effect=entries), \
             mock.patch.object(self.server, "workspace_digest", side_effect=AssertionError("unexpected media read")), \
             mock.patch.object(self.server, "normalize_timeline", side_effect=AssertionError("unexpected normalization")):
            result = self.server.workspace_library()
        self.assertEqual(json.dumps(documents, sort_keys=True), before)
        return {(entry["project_slug"], entry["id"]): entry["used_in_count"] for entry in result["assets"]}

    @staticmethod
    def project(slug: str, *, source: dict | None = None, uses: int = 0) -> dict:
        media = {"id": "video", "kind": "video"}
        if source:
            media["imported_from"] = source
        return {"slug": slug, "asset": {"media": [media], "clips": [{"id": "prepared", "media_id": "video"}]},
                "assembly": {"timeline": {"tracks": [{"id": "video-main", "kind": "video",
                    "clips": [{"clip_id": "prepared"} for _ in range(uses)]}]}}}

    def test_imported_video_counts_used_projects_once(self) -> None:
        source = self.project("source", uses=2)
        target = self.project("target", source={"project": "source", "asset": "video"}, uses=3)
        counts = self.library([source, target])
        self.assertEqual(set(counts.values()), {2})

    def test_imported_video_in_library_without_timeline_use_does_not_count(self) -> None:
        source = self.project("source", uses=1)
        target = self.project("target", source={"project": "source", "asset": "clip:prepared"})
        counts = self.library([source, target])
        self.assertEqual(set(counts.values()), {1})
        source["assembly"]["timeline"]["tracks"][0]["clips"] = []
        self.assertEqual(set(self.library([source, target]).values()), {0})

    def test_nested_imports_legacy_order_and_direct_media_use(self) -> None:
        source = self.project("source")
        source["assembly"] = {"order": ["prepared", "prepared"]}
        middle = self.project("middle", source={"project": "source", "asset": "clip:prepared"})
        target = self.project("target", source={"project": "middle", "asset": "video"})
        target["assembly"]["timeline"]["tracks"].append({"id": "audio-main", "kind": "audio", "clips": [{"media_id": "video"}]})
        self.assertEqual(set(self.library([target, middle, source]).values()), {2})

    def test_canonical_empty_track_ignores_stale_legacy_order(self) -> None:
        document = self.project("source")
        document["assembly"]["order"] = ["prepared"]
        self.assertEqual(set(self.library([document]).values()), {0})

    def test_import_lineage_cycle_is_safe_and_does_not_mix_same_ids(self) -> None:
        first = self.project("first", source={"project": "second", "asset": "video"}, uses=1)
        second = self.project("second", source={"project": "first", "asset": "video"})
        unrelated = self.project("unrelated")
        counts = self.library([first, second, unrelated])
        self.assertEqual(counts[("first", "video")], 1)
        self.assertEqual(counts[("second", "video")], 1)
        self.assertEqual(counts[("unrelated", "video")], 0)


if __name__ == "__main__":
    unittest.main()
