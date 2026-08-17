"""Regression tests for the duplicate i18n key checker.

Run with: python .github/scripts/test_check_i18n_duplicate_keys.py
"""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / ".github/scripts/check_i18n_duplicate_keys.py"


class CheckI18nDuplicateKeysTests(unittest.TestCase):
    def run_checker(self, frontend_root: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CHECKER), str(frontend_root)],
            check=False,
            capture_output=True,
            text=True,
        )

    def write_fixture(self, root: Path, relative_path: str, contents: str) -> None:
        fixture_path = root / relative_path
        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        fixture_path.write_text(contents, encoding="utf-8")

    def test_duplicate_top_level_key_fails_with_key_and_both_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            frontend_root = Path(temp_dir)
            self.write_fixture(
                frontend_root,
                "i18n/translations.json",
                '''{
  "Common.Delete": {"de": "Löschen", "en": "Delete", "fr": "Supprimer"},
  "Common.Delete": {"de": "Entfernen", "en": "Remove", "fr": "Retirer"}
}''',
            )

            result = self.run_checker(frontend_root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Common.Delete", result.stderr)
        self.assertIn("Delete", result.stderr)
        self.assertIn("Remove", result.stderr)

    def test_repeated_nested_language_keys_in_distinct_entries_pass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            frontend_root = Path(temp_dir)
            self.write_fixture(
                frontend_root,
                "i18n/translations.json",
                '''{
  "Common.Delete": {"de": "Löschen", "en": "Delete", "fr": "Supprimer"},
  "Common.Save": {"de": "Speichern", "en": "Save", "fr": "Enregistrer"}
}''',
            )

            result = self.run_checker(frontend_root)

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_key_duplicated_three_times_reports_every_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            frontend_root = Path(temp_dir)
            self.write_fixture(
                frontend_root,
                "i18n/translations.json",
                '''{
  "Common.Delete": {"en": "Delete"},
  "Common.Delete": {"en": "Remove"},
  "Common.Delete": {"en": "Erase"}
}''',
            )

            result = self.run_checker(frontend_root)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Common.Delete: 3 occurrences", result.stderr)
        self.assertIn("Delete", result.stderr)
        self.assertIn("Remove", result.stderr)
        self.assertIn("Erase", result.stderr)

    def test_missing_translations_file_passes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = self.run_checker(Path(temp_dir))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No translations file found", result.stdout)

    def test_both_supported_directory_layouts_are_found(self):
        for relative_path in (
            "i18n/translations.json",
            "src/i18n/translations.json",
        ):
            with self.subTest(relative_path=relative_path), tempfile.TemporaryDirectory() as temp_dir:
                frontend_root = Path(temp_dir)
                self.write_fixture(
                    frontend_root,
                    relative_path,
                    '''{
  "Layout.Check": {"en": "first"},
  "Layout.Check": {"en": "second"}
}''',
                )

                result = self.run_checker(frontend_root)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Layout.Check", result.stderr)


if __name__ == "__main__":
    unittest.main()
