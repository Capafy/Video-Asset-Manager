"""Tests for generator context continuity."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import vpm_context  # noqa: E402
import vpm_prepare  # noqa: E402


class ContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="vam-context-")
        self.root = Path(self.temp.name) / "manager"
        self.slug = "project-test"
        project = self.root / self.slug
        (project / "logs").mkdir(parents=True)
        (project / "project.json").write_text(json.dumps({
            "schema": 2, "rev": 7, "slug": self.slug, "title": "Test project", "status": "reviewing",
            "asset": {"scripts": [], "media": [], "clips": [], "finals": []},
            "assembly": {"order": [], "timeline": {"tracks": []}},
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_log(self, rows: list[dict]) -> None:
        path = self.root / self.slug / "logs" / "edits.log"
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    def test_read_filters_noise_and_keeps_user_intent(self) -> None:
        self.write_log([
            {"op": "move", "item_id": "x", "start": 1},
            {"op": "set_speed", "item_id": "x", "speed": 1.2},
            {"op": "set_speed", "item_id": "x", "speed": 0.8},
            {"op": "clip.trim", "clip": "clip1", "in": 0.5, "out": 2.0, "new_version": 2, "file": "clips/clip1/v2.mp4"},
            {"op": "intent.message", "text": "继续生成下一段"},
            {"op": "asset.upload", "id": "a1", "name": "ref.png", "path": "assets/uploads/ref.png"},
        ])
        result = vpm_context.read_context(self.root, self.slug)
        self.assertTrue(result["ok"])
        self.assertEqual(result["cursor"]["next_line"], 6)
        self.assertEqual(len(result["summary"]["clip_trims"]), 1)
        self.assertEqual(result["summary"]["property_changes"][0]["last"]["speed"], 0.8)
        self.assertEqual(result["summary"]["draft_messages"][0]["text"], "继续生成下一段")
        self.assertEqual(result["summary"]["new_materials"][0]["path"], "assets/uploads/ref.png")

    def test_malformed_trailing_line_is_not_acknowledged_as_event(self) -> None:
        path = self.root / self.slug / "logs" / "edits.log"
        path.write_text('{"op":"clip.trim","clip":"c"}\n{"op":"set_speed"', encoding="utf-8")
        result = vpm_context.read_context(self.root, self.slug)
        self.assertEqual(result["cursor"]["next_line"], 1)
        self.assertTrue(result["cursor"]["warnings"])

    def test_ack_is_digest_protected_and_idempotent(self) -> None:
        self.write_log([{ "op": "assembly.reorder", "order": ["c"] }])
        context = vpm_context.read_context(self.root, self.slug)
        ack = vpm_context.ack_context(self.root, self.slug, context["cursor"])
        self.assertTrue(ack["ok"])
        repeated = vpm_context.ack_context(self.root, self.slug, context["cursor"])
        self.assertTrue(repeated["idempotent"])
        path = self.root / self.slug / "logs" / "edits.log"
        path.write_text(json.dumps({"op": "remove"}) + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaises(ValueError):
            vpm_context.ack_context(self.root, self.slug, context["cursor"])

    def test_prepare_returns_previous_active_project_context(self) -> None:
        (self.root / "active_project").write_text(self.slug, encoding="utf-8")
        self.write_log([{ "op": "clip.trim", "clip": "c", "in": 1, "out": 2 }])
        result = vpm_prepare.prepare({"title": "Test project", "script": "new request", "task_id": "task-new"}, self.root, open_window=False)
        self.assertEqual(result["model_context"]["project_slug"], self.slug)
        self.assertEqual(len(result["model_context"]["summary"]["clip_trims"]), 1)


# --- manager root -----------------------------------------------------------------
#
# Every entry point resolves the manager root on its own, so the precedence order
# is a contract between them: a launcher that disagrees with the service would
# write a project where the page never looks.  These tests fail as soon as one
# copy of the order drifts.


def _app_dir() -> Path | None:
    """Directory holding server.py and ensure.py, in either shipped layout."""

    for candidate in (SCRIPTS, SCRIPTS.parent / "assets" / "webapp"):
        if (candidate / "server.py").is_file() and (candidate / "ensure.py").is_file():
            return candidate
    return None


def _norm(value: object) -> str:
    """Compare roots platform-independently (Windows renders '/' paths with '\\')."""

    return os.path.normcase(str(value).replace("\\", "/").rstrip("/"))


# Normalised once, because normcase rewrites separators and case on Windows.
CAPAFY_WORKSPACE_DIR = _norm("/home/user/workspace")
CAPAFY_PROJECTS_DIR = _norm("/home/user/workspace/projects")


APP_DIR = _app_dir()
ROOT_RESOLVERS: list[tuple[str, object]] = []
IMPORT_ERROR: str | None = None

try:
    import handoff_impl  # noqa: E402
    import vpm_record  # noqa: E402
    import vpm_sync  # noqa: E402

    if APP_DIR is not None and str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    import ensure  # noqa: E402
    import server  # noqa: E402

    ROOT_RESOLVERS = [
        ("assets/webapp/server.py default_root", server.default_root),
        ("assets/webapp/ensure.py resolve_root", ensure.resolve_root),
        ("scripts/vpm_context.py resolve_root", lambda: vpm_context.resolve_root(None)),
        ("scripts/vpm_prepare.py resolve_root", lambda: vpm_prepare.resolve_root(None)),
        ("scripts/vpm_record.py default_root", vpm_record.default_root),
        ("scripts/vpm_sync.py resolve_root", vpm_sync.resolve_root),
        ("scripts/handoff_impl.py default_root", handoff_impl.default_root),
    ]
except Exception as exc:  # pragma: no cover - a damaged or partial install
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


@unittest.skipIf(not ROOT_RESOLVERS, f"entry points unavailable ({IMPORT_ERROR})")
class ManagerRootLockstepTests(unittest.TestCase):
    """The manager root precedence order is identical in every entry point."""

    MANAGED = ("VIDEO_ASSET_MANAGER_ROOT", "VPM_ROOT", "CAPAFY_WORKSPACE")

    def setUp(self) -> None:
        self.saved = {name: os.environ.pop(name, None) for name in self.MANAGED}

    def tearDown(self) -> None:
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def resolve_all(self) -> dict[str, object]:
        return {name: resolve() for name, resolve in ROOT_RESOLVERS}

    def assert_agree(self, expected: str, values: dict[str, object]) -> None:
        distinct = {_norm(value) for value in values.values()}
        self.assertEqual(len(distinct), 1, f"entry points disagree: {values}")
        self.assertEqual(distinct.pop(), _norm(expected), values)

    def test_shared_projects_path_wins_when_its_parent_exists(self) -> None:
        real_isdir, real_makedirs = os.path.isdir, os.makedirs
        created: list[str] = []

        def fake_isdir(path: object) -> bool:
            location = _norm(path)
            if location == CAPAFY_WORKSPACE_DIR:
                return True
            if location == CAPAFY_PROJECTS_DIR:
                return False
            return real_isdir(path)

        def fake_makedirs(path: object, *args: object, **kwargs: object) -> None:
            created.append(str(path))

        os.path.isdir, os.makedirs = fake_isdir, fake_makedirs
        try:
            values = self.resolve_all()
        finally:
            os.path.isdir, os.makedirs = real_isdir, real_makedirs

        self.assert_agree("/home/user/workspace/projects", values)
        self.assertEqual(len(created), len(ROOT_RESOLVERS), created)
        self.assertTrue(all(_norm(path) == CAPAFY_PROJECTS_DIR for path in created), created)

    def test_shared_projects_path_is_used_when_it_already_exists(self) -> None:
        real_isdir = os.path.isdir

        def fake_isdir(path: object) -> bool:
            return True if _norm(path) == CAPAFY_PROJECTS_DIR else real_isdir(path)

        os.path.isdir = fake_isdir
        try:
            values = self.resolve_all()
        finally:
            os.path.isdir = real_isdir

        self.assert_agree("/home/user/workspace/projects", values)

    def test_explicit_root_override_wins_in_every_entry_point(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vam-root-") as temp:
            os.environ["VIDEO_ASSET_MANAGER_ROOT"] = temp
            self.assert_agree(temp, self.resolve_all())

    def test_legacy_root_override_wins_in_every_entry_point(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vam-root-legacy-") as temp:
            os.environ["VPM_ROOT"] = temp
            self.assert_agree(temp, self.resolve_all())

    def test_workspace_fallback_agrees_when_no_capafy_path_exists(self) -> None:
        real_isdir, real_makedirs = os.path.isdir, os.makedirs

        def fake_isdir(path: object) -> bool:
            location = _norm(path)
            if location == CAPAFY_WORKSPACE_DIR or location.startswith(CAPAFY_WORKSPACE_DIR + "/"):
                return False
            return real_isdir(path)

        os.path.isdir = fake_isdir
        os.makedirs = lambda *args, **kwargs: None
        try:
            with tempfile.TemporaryDirectory(prefix="vam-workspace-") as temp:
                os.environ["CAPAFY_WORKSPACE"] = temp
                self.assert_agree(Path(temp) / ".capafy" / "video-asset-manager", self.resolve_all())
        finally:
            os.path.isdir, os.makedirs = real_isdir, real_makedirs


if __name__ == "__main__":
    unittest.main()
