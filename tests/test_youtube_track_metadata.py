import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

try:
    from mutagen.id3 import ID3
except ModuleNotFoundError:
    ID3 = None


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "youtube_track_metadata.py"
SPEC = importlib.util.spec_from_file_location("youtube_track_metadata_test", MODULE_PATH)
metadata = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(metadata)


class YouTubeTrackMetadataTests(unittest.TestCase):
    def test_prefers_song_metadata_over_playlist_and_uploader_fields(self):
        result = metadata.source_track_metadata(
            {
                "title": "Artist Name - Song Name (Official Audio)",
                "track": "Song Name",
                "artist": "Artist Name",
                "album": "Actual Album",
                "album_artist": "Artist Name",
                "release_date": "20260830",
                "track_number": 4,
                "playlist": "Wrong Playlist Name",
                "uploader": "YouTube Music",
                "genres": ["Electronic", "Trip Hop"],
            }
        )

        self.assertEqual(result["title"], "Song Name")
        self.assertEqual(result["artist"], "Artist Name")
        self.assertEqual(result["album"], "Actual Album")
        self.assertEqual(result["albumArtist"], "Artist Name")
        self.assertEqual(result["year"], "2026")
        self.assertEqual(result["trackNumber"], "4")
        self.assertEqual(result["genre"], "Electronic, Trip Hop")

    def test_generic_youtube_artist_is_replaced_from_video_title(self):
        result = metadata.source_track_metadata(
            {
                "title": "Real Artist - Real Song",
                "uploader": "YouTube Music",
            }
        )

        self.assertEqual(result["artist"], "Real Artist")
        self.assertEqual(result["title"], "Real Song")
        self.assertEqual(result["album"], "")

    def test_applies_individual_tags_and_artwork_and_removes_fake_album(self):
        if ID3 is None:
            self.skipTest("mutagen is installed on the Aria server runtime")
        with tempfile.TemporaryDirectory() as directory:
            mp3 = Path(directory) / "track.mp3"
            ID3().save(mp3)
            artwork = Path(directory) / "cover.jpg"
            artwork.write_bytes(b"individual-cover")

            metadata.apply_youtube_metadata(
                mp3,
                {
                    "track": "Own Title",
                    "artist": "Own Artist",
                    "release_year": 2025,
                },
                artwork_path=artwork,
            )

            tags = ID3(mp3)
            self.assertEqual(str(tags["TIT2"]), "Own Title")
            self.assertEqual(str(tags["TPE1"]), "Own Artist")
            self.assertNotIn("TALB", tags)
            self.assertEqual(str(tags["TDRC"]), "2025")
            self.assertEqual(tags.getall("APIC")[0].data, b"individual-cover")


if __name__ == "__main__":
    unittest.main()
