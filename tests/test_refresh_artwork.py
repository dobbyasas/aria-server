import importlib.util
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "aria_song_server.py"
SPEC = importlib.util.spec_from_file_location("aria_song_server_artwork", MODULE_PATH)
SERVER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SERVER)


class ArtworkRefreshTests(unittest.TestCase):
    def test_accepts_only_youtube_image_hosts(self):
        self.assertTrue(
            SERVER.is_youtube_artwork_url(
                "https://lh3.googleusercontent.com/example=w544-h544"
            )
        )
        self.assertTrue(
            SERVER.is_youtube_artwork_url("https://i.ytimg.com/vi/example/maxresdefault.jpg")
        )
        self.assertFalse(SERVER.is_youtube_artwork_url("http://i.ytimg.com/cover.jpg"))
        self.assertFalse(SERVER.is_youtube_artwork_url("https://ytimg.com.example.org/cover.jpg"))

    def test_replaces_album_artwork_and_preserves_catalog_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            songs_dir = Path(temporary_directory)
            song_path = songs_dir / "01 - Song.mp3"
            song_path.write_bytes(b"existing audio")

            track_id = str(uuid.uuid4())
            record = {
                "id": track_id,
                "filename": song_path.name,
                "title": "Edited Song Title",
                "artist": "Artist",
                "albumArtist": "Artist",
                "album": "Album",
                "duration": 120,
                "year": 2026,
                "trackNumber": 1,
                "hasArtwork": False,
                "isExplicit": False,
                "artwork": SERVER.palette_for(song_path),
                "size": song_path.stat().st_size,
                "mtimeNs": song_path.stat().st_mtime_ns,
            }
            record["searchText"] = SERVER.search_text_for(record)
            record["albumID"] = SERVER.album_id_for("Album")

            catalog = SERVER.CatalogIndex(songs_dir)
            catalog.records = [record]
            catalog.records_by_filename = {song_path.name: record}

            jpeg = b"\xff\xd8\xff\xe0fresh-cover"
            with patch.object(SERVER, "replace_embedded_artwork") as replace_artwork:
                refreshed, count = catalog.refresh_album_artwork(
                    track_id,
                    jpeg,
                    "image/jpeg",
                )

            self.assertEqual(count, 1)
            self.assertEqual(refreshed["title"], "Edited Song Title")
            self.assertTrue(refreshed["hasArtwork"])
            replace_artwork.assert_called_once_with(song_path, jpeg, "image/jpeg")
            self.assertIn(
                "?v=",
                SERVER.artwork_url_for_record(refreshed, "http://aria.example"),
            )


if __name__ == "__main__":
    unittest.main()
