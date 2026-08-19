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
    def make_candidate(self, install_root, name, version):
        candidate = install_root / "versions" / name
        executable = candidate / "bin" / "yt-dlp"
        executable.parent.mkdir(parents=True)
        executable.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
        executable.chmod(0o755)
        return candidate

    def test_activation_keeps_previous_validated_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "managed"
            install_root.mkdir()

            first_candidate = self.make_candidate(
                install_root, "candidate-first", "2026.08.19"
            )
            first = update_downloader.activate_candidate(
                first_candidate, install_root, "2026.08.19", keep=3
            )

            second_candidate = self.make_candidate(
                install_root, "candidate-second", "2026.08.20"
            )
            second = update_downloader.activate_candidate(
                second_candidate, install_root, "2026.08.20", keep=3
            )

            self.assertEqual((install_root / "current").resolve(), second)
            self.assertEqual((install_root / "previous").resolve(), first)

    def test_failed_launcher_restores_previous_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "managed"
            install_root.mkdir()
            working = self.make_candidate(install_root, "working", "2026.08.19")
            working_target = update_downloader.activate_candidate(
                working, install_root, "2026.08.19", keep=3
            )

            broken = self.make_candidate(install_root, "broken", "2026.08.20")
            executable = broken / "bin" / "yt-dlp"
            executable.write_text("#!/missing/python\n")

            with self.assertRaises(update_downloader.UpdateError):
                update_downloader.activate_candidate(
                    broken, install_root, "2026.08.20", keep=3
                )

            self.assertEqual((install_root / "current").resolve(), working_target)

    def test_activation_refuses_to_replace_a_real_current_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            install_root = Path(temp_dir) / "managed"
            current = install_root / "current"
            current.mkdir(parents=True)
            candidate = self.make_candidate(install_root, "candidate", "2026.08.19")

            with self.assertRaises(update_downloader.UpdateError):
                update_downloader.activate_candidate(candidate, install_root, "2026.08.19", keep=3)


if __name__ == "__main__":
    unittest.main()
