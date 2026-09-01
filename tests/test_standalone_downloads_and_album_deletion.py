import importlib.util
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "aria_song_server.py"
SPEC = importlib.util.spec_from_file_location("aria_song_server_standalone", MODULE_PATH)
server = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(server)


class StandaloneDownloadsAndAlbumDeletionTests(unittest.TestCase):
    def record(self, path: Path, *, album: str, standalone: bool) -> dict:
        stat = path.stat()
        record = {
            "filename": path.name,
            "size": stat.st_size,
            "mtimeNs": stat.st_mtime_ns,
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, path.name)),
            "title": path.stem,
            "artist": "Test Artist",
            "albumArtist": "Test Artist",
            "album": album,
            "duration": 120,
            "year": 2026,
            "trackNumber": 1,
            "artwork": server.palette_for(path),
            "hasArtwork": False,
            "isExplicit": False,
            "isStandalone": standalone,
        }
        record["searchText"] = server.search_text_for(record)
        record["albumID"] = server.album_id_for(album)
        return record

    def test_standalone_manifest_updates_cached_record_without_retagging_file(self):
        with tempfile.TemporaryDirectory() as directory:
            songs = Path(directory)
            track = songs / "Artist - Single [video].mp3"
            track.write_bytes(b"audio")
            index = server.CatalogIndex(songs)
            record = self.record(track, album="A Real Album", standalone=False)
            index.save([record])

            server.save_standalone_track_filenames(songs, {track.name})
            index.refresh(force=True)

            refreshed = index.track_for_id(record["id"])
            self.assertIsNotNone(refreshed)
            self.assertTrue(refreshed["isStandalone"])

    def test_delete_album_removes_only_album_files_and_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            songs = Path(directory)
            album_track = songs / "01 - Album Song.mp3"
            standalone_track = songs / "Artist - Single [video].mp3"
            sidecar = album_track.with_suffix(".lrc")
            album_track.write_bytes(b"album")
            standalone_track.write_bytes(b"single")
            sidecar.write_text("lyrics", encoding="utf-8")

            album_record = self.record(album_track, album="Shared Name", standalone=False)
            standalone_record = self.record(standalone_track, album="Shared Name", standalone=True)
            index = server.CatalogIndex(songs)
            index.records = [album_record, standalone_record]
            index.records_by_filename = {
                record["filename"]: record for record in index.records
            }
            index.save(index.records)
            server.save_standalone_track_filenames(songs, {standalone_track.name})

            result = index.delete_album(album_record["albumID"])

            self.assertEqual(result, (1, {album_record["id"]}))
            self.assertFalse(album_track.exists())
            self.assertFalse(sidecar.exists())
            self.assertTrue(standalone_track.exists())
            manifest = json.loads(
                (songs / server.STANDALONE_TRACK_STORE_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["filenames"], [standalone_track.name])

    def test_playlist_cleanup_removes_deleted_track_ids_and_increments_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = server.PlaylistManager(Path(directory))
            playlist_id = str(uuid.uuid4())
            deleted_id = str(uuid.uuid4())
            kept_id = str(uuid.uuid4())
            manager.upsert(
                playlist_id,
                {
                    "title": "Test",
                    "trackIDs": [deleted_id, kept_id],
                    "revision": 4,
                },
            )

            changed = manager.remove_track_ids({deleted_id})
            playlist = manager.all()[0]

            self.assertEqual(changed, 1)
            self.assertEqual(playlist["trackIDs"], [kept_id])
            self.assertEqual(playlist["revision"], 5)

    def test_download_manager_accepts_standalone_modes_without_album_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            songs = base / "songs"
            songs.mkdir()
            manager = server.DownloadManager(base, songs, server.CatalogIndex(songs))
            manager.run_job = lambda job: manager.clear_active(job)

            song_job = manager.start({"link": "https://music.youtube.com/watch?v=x", "kind": "song"})
            self.assertEqual(song_job.kind, "song")
            while manager.active_job() is not None:
                pass

            playlist_job = manager.start({"link": "https://music.youtube.com/playlist?list=x", "kind": "playlist"})
            self.assertEqual(playlist_job.kind, "playlist")

    def test_playlist_matching_reuses_youtube_id_then_title_and_artist(self):
        records = [
            {
                "id": str(uuid.uuid4()),
                "filename": "Artist - Existing [abcdefghijk].mp3",
                "title": "Existing",
                "artist": "Artist",
                "albumArtist": "Artist",
            },
            {
                "id": str(uuid.uuid4()),
                "filename": "01 - Album Song.mp3",
                "title": "Album Song (Remastered)",
                "artist": "Album Artist",
                "albumArtist": "Album Artist",
            },
        ]

        by_id = server.DownloadManager.match_entry(
            {"id": "abcdefghijk", "title": "Different title", "artist": "Different"},
            records,
        )
        by_metadata = server.DownloadManager.match_entry(
            {"id": "missing-id", "title": "Album Song", "artist": "Album Artist - Topic"},
            records,
        )
        by_youtube_style = server.DownloadManager.match_entry(
            {"id": "missing-id", "title": "Album Artist - Album Song (Official Video)", "artist": "albumartist"},
            records,
        )
        by_unique_title = server.DownloadManager.match_entry(
            {"id": "missing-id", "title": "Album Song", "artist": "Original Artist"},
            records,
        )

        self.assertEqual(by_id["id"], records[0]["id"])
        self.assertEqual(by_metadata["id"], records[1]["id"])
        self.assertEqual(by_youtube_style["id"], records[1]["id"])
        self.assertEqual(by_unique_title["id"], records[1]["id"])

    def test_only_standalone_or_legacy_playlist_records_are_retagged(self):
        self.assertTrue(server.DownloadManager.is_legacy_playlist_record({
            "album": "Playlist",
            "artist": "YouTube Music",
        }))
        self.assertTrue(server.DownloadManager.is_legacy_playlist_record({
            "album": "Anything",
            "albumArtist": "YouTube Music",
        }))
        self.assertFalse(server.DownloadManager.is_legacy_playlist_record({
            "album": "Actual Album",
            "albumArtist": "Actual Artist",
        }))

    def test_inspection_and_playlist_creation_reuse_existing_catalog_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            songs = base / "songs"
            songs.mkdir()
            catalog = server.CatalogIndex(songs)
            playlists = server.PlaylistManager(songs)
            manager = server.DownloadManager(base, songs, catalog, playlists)
            job = server.DownloadJob(
                link="https://music.youtube.com/playlist?list=test",
                album="My Mix",
                album_artist="Curator",
                year="",
                kind="playlist",
            )
            inspected_payload = {
                "entries": [
                    {"id": "video000001", "title": "First", "uploader": "One - Topic"},
                    {"id": "video000002", "title": "Second", "artist": "Two"},
                ]
            }
            completed = server.subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=json.dumps(inspected_payload),
                stderr="",
            )
            with patch.object(server.subprocess, "run", return_value=completed):
                entries = manager.inspect_entries(job)

            records = [
                {
                    "id": str(uuid.uuid4()),
                    "filename": "One - First [video000001].mp3",
                    "title": "First",
                    "artist": "One",
                    "albumArtist": "One",
                },
                {
                    "id": str(uuid.uuid4()),
                    "filename": "Two - Second [video000002].mp3",
                    "title": "Second",
                    "artist": "Two",
                    "albumArtist": "Two",
                },
            ]
            manager.create_downloaded_playlist(job, entries, records)
            created = playlists.all()[0]

            self.assertEqual([entry["artist"] for entry in entries], ["One", "Two"])
            self.assertEqual(created["title"], "My Mix")
            self.assertEqual(created["trackIDs"], [record["id"] for record in records])
            self.assertEqual(job.playlist_track_count, 2)


if __name__ == "__main__":
    unittest.main()
