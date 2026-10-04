"""Real installed-package fixture setup with the runner copied alone into /tmp.

This stdlib-only suite also runs directly in the app image (no pytest or source
checkout). Set COGNITA_SELFTEST_RUNNER to the copied runner for that probe.
"""
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cognita.registry import Project, Registry
from cognita.selftest_fixtures import load_manifest


class PackagedFixtureSetup(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cognita-http-fixture-")
        self.root = Path(temporary.name)
        def cleanup():
            temporary.cleanup()
            self.assertFalse(self.root.exists(), "owned fixture root remains")
        self.addCleanup(cleanup)
        source = Path(os.environ.get("COGNITA_SELFTEST_RUNNER",
                      str(Path(__file__).resolve().parents[1] / "scripts/run-selftest.py")))
        copied = self.root / "cognita-run-selftest.py"
        shutil.copyfile(source, copied)
        spec = importlib.util.spec_from_file_location("copied_selftest_runner", copied)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)
        self.config = self.root / "config"
        self.docs = self.root / "projects" / "Self-Test"
        self.data = self.config / "data" / "Self-Test"
        self.docs.mkdir(parents=True)
        self.data.mkdir(parents=True)
        self.registry = Registry(self.config / "registry.yaml")
        self.registry.add(Project(name="Self-Test", documents_dir=self.docs, data_dir=self.data))
        environment = patch.dict(os.environ, {"COGNITA_CONFIG_ROOT": str(self.config)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_installed_allowlist_provisions_empty_project_without_touching_canary(self):
        canary = self.docs / "saved-report.md"
        canary.write_bytes(b"synthetic saved report\n")
        canary_before = canary.stat()
        registry_before = self.registry.path.read_bytes()
        result = self.runner._provision_ocr_fixtures("Self-Test")
        fixtures = load_manifest()
        self.assertFalse(result.project_created)
        self.assertEqual(set(result.copied), {row.path for row in fixtures})
        self.assertEqual(len(result.verified), 6)
        self.assertEqual(self.registry.path.read_bytes(), registry_before)
        canary_after = canary.stat()
        self.assertEqual((canary_before.st_size, canary_before.st_atime_ns,
                          canary_before.st_mtime_ns, canary_before.st_ctime_ns),
                         (canary_after.st_size, canary_after.st_atime_ns,
                          canary_after.st_mtime_ns, canary_after.st_ctime_ns))
        self.assertEqual(canary.read_bytes(), b"synthetic saved report\n")
        repeat = self.runner._provision_ocr_fixtures("Self-Test")
        self.assertEqual(repeat.copied, ())
        self.assertEqual(repeat.verified, result.verified)

    @unittest.skipUnless(hasattr(os, "O_NOATIME"), "Linux packaged receipt verification")
    def test_exact_missing_file_then_real_receipts_after_provisioning(self):
        fixtures = load_manifest()
        root = self.runner._ocr_fixture_root("Self-Test")
        with self.assertRaises(FileNotFoundError) as failure:
            self.runner._ocr_fixture_receipts(root, fixtures)
        self.assertEqual(failure.exception.filename, str(self.docs / fixtures[0].path))
        self.assertEqual(fixtures[0].path, "cognita-selftest/ocr/canonical-clear.png")
        score = self.runner.Scorecard()
        client = type("Scope", (), {"project": "Self-Test"})()
        self.assertFalse(self.runner.run_ocr_plan(client, score))
        self.assertTrue(any("phase=receipts type=FileNotFoundError fixture=" + fixtures[0].path in line
                            for line in score.lines))
        print("reproduced_missing_fixture=" + fixtures[0].path)
        self.runner._provision_ocr_fixtures("Self-Test")
        for row in fixtures:
            os.utime(root / row.path, ns=(1_000_000_000, 2_000_000_000))
        before = {row.path: (root / row.path).stat() for row in fixtures}
        receipts = self.runner._ocr_fixture_receipts(root, fixtures)
        self.assertEqual(receipts, self.runner._ocr_fixture_receipts(root, fixtures))
        for row in fixtures:
            self.assertEqual(receipts[row.path]["sha256"], row.sha256)
            after = (root / row.path).stat()
            prior = before[row.path]
            self.assertEqual((after.st_atime_ns, after.st_mtime_ns, after.st_ctime_ns),
                             (prior.st_atime_ns, prior.st_mtime_ns, prior.st_ctime_ns))

    def test_unavailable_or_wrong_project_is_not_created_or_provisioned(self):
        for state in ("wrong", "disabled", "read-only", "missing"):
            with self.subTest(state=state):
                project = self.registry.get("Self-Test")
                if state == "disabled":
                    project.enabled = False
                if state == "read-only":
                    project.enabled, project.writable = True, False
                if state == "missing":
                    self.registry.projects = []
                self.registry.save()
                before = self.registry.path.read_bytes()
                with self.assertRaises(RuntimeError):
                    self.runner._provision_ocr_fixtures("Other-Project" if state == "wrong" else "Self-Test")
                self.assertEqual(self.registry.path.read_bytes(), before)
                self.assertEqual(list(self.docs.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "Linux symlink refusal")
    def test_linked_fixture_destination_is_refused_without_writing_target(self):
        target = self.root / "unrelated-synthetic"
        target.mkdir()
        (self.docs / "cognita-selftest").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            self.runner._provision_ocr_fixtures("Self-Test")
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
