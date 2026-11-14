"""Generic leak-guard tests: no project source or SCL fixture is embedded."""
import base64
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from check_publish import check_entries, fingerprints, inspect_blob, protected_fingerprints, revision_entries


class PublishGuardTests(unittest.TestCase):
    def setUp(self):
        self.marker = " ".join(f"privateToken{i}" for i in range(24))
        self.protected = fingerprints(self.marker)

    def test_renamed_and_reformatted_fragments_are_blocked(self):
        for text in [self.marker, self.marker.upper().replace(" ", "\n  ")]:
            errors = inspect_blob("README.md", text.encode(), self.protected)
            self.assertIn("fragment matches protected local source", errors)

    def test_literal_and_base64_encodings_are_blocked(self):
        escaped = "".join("\\u%04x" % ord(c) for c in self.marker)
        source = 'value = "' + escaped + '"'
        errors = inspect_blob("tools/scl_codegen.py", source.encode(), self.protected)
        self.assertIn("fragment matches protected local source", errors)
        encoded = base64.b64encode(self.marker.encode())
        self.assertTrue(inspect_blob("README.md", encoded, self.protected))

    def test_allowlist_binary_symlink_and_encoding_checks(self):
        for path, data, mode in [
            ("extra.txt", b"plain", "100644"),
            ("program.scl", b"plain", "100644"),
            ("README.md", b"\0binary", "100644"),
            ("README.md", b"\xff", "100644"),
            ("README.md", b"../external", "120000"),
        ]:
            with self.subTest(path=path, mode=mode):
                self.assertTrue(inspect_blob(path, data, set(), mode))
        self.assertEqual(inspect_blob("README.md", b"Ordinary documentation.", set()), [])

    def test_missing_or_unavailable_originals_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with self.assertRaises(OSError):
                protected_fingerprints(path)
            for content in ["[]", '["/unavailable-protected-project"]', '{}']:
                path.write_text(content, encoding="utf8")
                with self.assertRaises(ValueError):
                    protected_fingerprints(path)

    def test_deleted_historical_fragment_is_still_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def command(*args):
                return subprocess.run(["git", "-c", "user.name=Guard Test", "-c", "user.email=guard@example.invalid", *args],
                                      cwd=root, check=True, capture_output=True)
            command("init")
            (root / "README.md").write_text(self.marker, encoding="utf8")
            command("add", "README.md"); command("commit", "-m", "Temporary test data")
            (root / "README.md").write_text("Clean description", encoding="utf8")
            command("add", "README.md"); command("commit", "-m", "Remove test data")
            self.assertEqual(check_entries(revision_entries("HEAD", cwd=root), self.protected, cwd=root), [])
            self.assertTrue(check_entries(revision_entries("HEAD", history=True, cwd=root), self.protected, cwd=root))


if __name__ == "__main__":
    unittest.main()
