import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from server.aria_song_server import (
    AriaSongHandler,
    LyricsManager,
    lyrics_result,
    parse_synced_lyrics,
)


class StubLyricsManager(LyricsManager):
    def __init__(self, songs_dir, cache_dir):
        super().__init__(songs_dir, cache_dir)
        self.fetch_count = 0

    def fetch_from_lrclib(self, record):
        self.fetch_count += 1
        return lyrics_result(
            record["id"],
            "lrclib",
            plain_lyrics="First line\nSecond line",
            synced_lyrics="[00:01.20] First line\n[00:03.005] Second line",
        )


class LyricsTests(unittest.TestCase):
    def test_parses_multiple_timestamps_and_fraction_lengths(self):
        lines = parse_synced_lyrics(
            "[00:01.2] First\n[00:03.005][00:04.50] Second\n[ar:Artist]"
        )

        self.assertEqual([line["startTime"] for line in lines], [1.2, 3.005, 4.5])
        self.assertEqual([line["text"] for line in lines], ["First", "Second", "Second"])

    def test_sidecar_lyrics_are_preferred(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            songs_dir = Path(temp_dir) / "songs"
            songs_dir.mkdir()
            song = songs_dir / "track.mp3"
            song.write_bytes(b"audio")
            song.with_suffix(".lrc").write_text("[00:02.00] Sidecar line", encoding="utf-8")

            manager = StubLyricsManager(songs_dir, Path(temp_dir) / "cache")
            result = manager.lyrics_for(self.record(song))

            self.assertEqual(result["source"], "sidecar")
            self.assertTrue(result["isSynced"])
            self.assertEqual(result["syncedLines"][0]["text"], "Sidecar line")
            self.assertEqual(manager.fetch_count, 0)

    def test_online_lyrics_are_cached(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            songs_dir = Path(temp_dir) / "songs"
            songs_dir.mkdir()
            song = songs_dir / "track.mp3"
            song.write_bytes(b"audio")
            cache_dir = Path(temp_dir) / "cache"
            record = self.record(song)

            first_manager = StubLyricsManager(songs_dir, cache_dir)
            first = first_manager.lyrics_for(record)
            second_manager = StubLyricsManager(songs_dir, cache_dir)
            second = second_manager.lyrics_for(record)

            self.assertEqual(first, second)
            self.assertEqual(first_manager.fetch_count, 1)
            self.assertEqual(second_manager.fetch_count, 0)

    def test_lyrics_endpoint_returns_track_payload(self):
        record = {
            "id": "track-id",
            "filename": "track.mp3",
            "title": "Song",
            "artist": "Artist",
        }

        class FakeCatalogIndex:
            def track_for_id(self, track_id):
                return record if track_id == record["id"] else None

        class FakeLyricsManager:
            def lyrics_for(self, requested_record):
                return lyrics_result(
                    requested_record["id"],
                    "embedded",
                    plain_lyrics="A lyric",
                )

        handler = object.__new__(AriaSongHandler)
        handler.server = SimpleNamespace(
            catalog_index=FakeCatalogIndex(),
            lyrics_manager=FakeLyricsManager(),
        )
        captured = {}
        handler.write_json = lambda payload, status=None: captured.update(payload=payload)
        handler.send_error = lambda status, message: captured.update(error=(status, message))
        handler.write_lyrics(urlparse("/api/tracks/track-id/lyrics"))
        payload = captured["payload"]

        self.assertTrue(payload["available"])
        self.assertEqual(payload["trackID"], "track-id")
        self.assertEqual(payload["plainLyrics"], "A lyric")

    def record(self, song):
        return {
            "id": "track-id",
            "filename": song.name,
            "mtimeNs": song.stat().st_mtime_ns,
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "duration": 180,
        }


if __name__ == "__main__":
    unittest.main()
