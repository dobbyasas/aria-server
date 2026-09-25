import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import Mock

import test_standalone_downloads_and_album_deletion as fixtures

server = fixtures.server


class SongDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.songs = Path(self.temp.name)
        self.target = self.songs / 'Disliked [abcdefghijk].mp3'
        self.kept = self.songs / 'Kept [123456789ab].mp3'
        self.target.write_bytes(b'disliked')
        self.kept.write_bytes(b'kept')
        self.target.with_suffix('.lrc').write_text('lyrics')
        helper = fixtures.StandaloneDownloadsAndAlbumDeletionTests()
        self.records = [helper.record(path, album='Same Album', standalone=False)
                        for path in [self.target, self.kept]]
        self.catalog = server.CatalogIndex(self.songs)
        self.catalog.records = self.records
        self.catalog.records_by_filename = {r['filename']: r for r in self.records}
        self.catalog.save(self.records)
        server.save_standalone_track_filenames(self.songs, {self.target.name})
        self.playlists = server.PlaylistManager(self.songs)
        self.playlists.upsert(str(uuid.uuid4()), {'title': 'Both', 'trackIDs': [r['id'] for r in self.records]})
        self.downloads = SimpleNamespace(lock=threading.RLock(), active_job=lambda: None)
        self.handler = object.__new__(server.AriaSongHandler)
        self.handler.server = SimpleNamespace(catalog_index=self.catalog, playlist_manager=self.playlists,
                                              download_manager=self.downloads)
        self.handler.write_json = Mock()
        self.url = urlparse('/api/tracks/' + self.records[0]['id'])

    def test_delete_single_song_keeps_album_sibling_and_cleans_manifest_and_playlists(self):
        self.handler.delete_track(self.url)
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.with_suffix('.lrc').exists())
        self.assertTrue(self.kept.exists())
        self.assertEqual(self.playlists.all()[0]['trackIDs'], [self.records[1]['id']])
        self.assertNotIn(self.target.name, server.standalone_track_filenames(self.songs))
        self.assertEqual(self.handler.write_json.call_args.args[0]['deletedFiles'], 1)

    def test_repeated_delete_is_successful(self):
        self.handler.delete_track(self.url)
        self.handler.delete_track(self.url)
        self.assertEqual(self.handler.write_json.call_args.args[0]['deletedFiles'], 0)

    def test_busy_download_leaves_song_and_playlists_untouched(self):
        self.downloads.active_job = lambda: object()
        self.handler.delete_track(self.url)
        self.assertTrue(self.target.exists())
        self.assertEqual(len(self.playlists.all()[0]['trackIDs']), 2)
        self.assertEqual(self.handler.write_json.call_args.kwargs['status'], 409)

    def test_invalid_id_does_not_delete_files(self):
        self.handler.delete_track(urlparse('/api/tracks/../Disliked.mp3'))
        self.assertTrue(self.target.exists())
        self.assertEqual(self.handler.write_json.call_args.kwargs['status'], 400)

    def test_unsafe_catalog_filename_cannot_delete_outside_songs(self):
        self.records[0]['filename'] = '../outside.mp3'
        self.handler.delete_track(self.url)
        self.assertTrue(self.target.exists())
        self.assertEqual(self.handler.write_json.call_args.kwargs['status'], 500)

    def test_delete_route_targets_song_not_album(self):
        self.handler.path = self.url.path
        self.handler.delete_track = Mock()
        self.handler.delete_track_album = Mock()
        self.handler.do_DELETE()
        self.handler.delete_track.assert_called_once()
        self.handler.delete_track_album.assert_not_called()


if __name__ == '__main__':
    unittest.main()
