import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "update_downloader.py"
SPEC = importlib.util.spec_from_file_location("update_downloader", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
update_downloader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update_downloader)


class UpdateDownloaderTests(unittest.TestCase):
    def test_safe_version_removes_path_characters(self):
        self.assertEqual(update_downloader.safe_version("nightly/2026 08"), "nightly-2026-08")

    def test_activation_keeps_previous_validated_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "managed"
            install_root.mkdir()

            first_candidate = install_root / ".candidate-first"
            first_candidate.mkdir()
            first = update_downloader.activate_candidate(
                first_candidate, install_root, "2026.08.19", keep=3
            )

            second_candidate = install_root / ".candidate-second"
            second_candidate.mkdir()
            second = update_downloader.activate_candidate(
                second_candidate, install_root, "2026.08.20", keep=3
            )

            self.assertEqual((install_root / "current").resolve(), second)
            self.assertEqual((install_root / "previous").resolve(), first)

    def test_activation_refuses_to_replace_a_real_current_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "managed"
            current = install_root / "current"
            current.mkdir(parents=True)
            candidate = install_root / ".candidate"
            candidate.mkdir()

            with self.assertRaises(update_downloader.UpdateError):
                update_downloader.activate_candidate(candidate, install_root, "2026.08.19", keep=3)


if __name__ == "__main__":
    unittest.main()
