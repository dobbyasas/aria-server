import base64
import importlib.util
import tempfile
import unittest
import uuid
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "aria_song_server.py"
SPEC = importlib.util.spec_from_file_location("aria_song_server_playlists", MODULE_PATH)
SERVER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SERVER)


class PlaylistManagerTests(unittest.TestCase):
    def test_playlist_round_trip_and_delete(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            songs_dir = Path(temporary_directory)
            manager = SERVER.PlaylistManager(songs_dir)
            playlist_id = str(uuid.uuid4())
            first_track_id = str(uuid.uuid4())
            second_track_id = str(uuid.uuid4())
            cover = base64.b64encode(b"cover data").decode("ascii")

            saved = manager.upsert(
                playlist_id,
                {
                    "title": "Shared Playlist",
                    "trackIDs": [first_track_id, first_track_id, second_track_id],
                    "coverImageData": cover,
                    "revision": 2,
                },
            )

            self.assertEqual(saved["id"], playlist_id)
            self.assertEqual(saved["trackIDs"], [first_track_id, second_track_id])
            self.assertEqual(saved["coverImageData"], cover)
            self.assertEqual(saved["revision"], 2)

            reloaded = SERVER.PlaylistManager(songs_dir)
            self.assertEqual(reloaded.all(), [saved])
            self.assertTrue(reloaded.delete(playlist_id))
            self.assertEqual(SERVER.PlaylistManager(songs_dir).all(), [])

    def test_rejects_oversized_cover(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = SERVER.PlaylistManager(Path(temporary_directory))
            oversized = base64.b64encode(
                b"x" * (SERVER.MAX_PLAYLIST_COVER_BYTES + 1)
            ).decode("ascii")

            with self.assertRaisesRegex(ValueError, "too large"):
                manager.upsert(
                    str(uuid.uuid4()),
                    {
                        "title": "Too Large",
                        "trackIDs": [],
                        "coverImageData": oversized,
                    },
                )


if __name__ == "__main__":
    unittest.main()
