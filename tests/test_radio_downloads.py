import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlparse

import test_standalone_downloads_and_album_deletion as fixtures

server = fixtures.server


class RadioDownloadsTests(unittest.TestCase):
    def setUp(self):
        refresh = patch.object(server.CatalogIndex, "refresh_in_background")
        refresh.start()
        self.addCleanup(refresh.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.songs = Path(self.temp.name)
        self.existing = self.songs / 'Existing [existing123].mp3'
        self.existing.write_bytes(b'existing audio')
        helper = fixtures.StandaloneDownloadsAndAlbumDeletionTests()
        self.existing_record = helper.record(self.existing, album='Album', standalone=False)
        self.catalog = server.CatalogIndex(self.songs)
        self.catalog.save([self.existing_record])
        self.catalog.refresh(force=True)
        self.playlists = server.PlaylistManager(self.songs)
        self.manager = server.DownloadManager(self.songs, self.songs, self.catalog, self.playlists)
        self.handler = object.__new__(server.AriaSongHandler)
        self.handler.server = SimpleNamespace(catalog_index=self.catalog, playlist_manager=self.playlists, download_manager=self.manager)
        self.handler.write_json = Mock()

    def download(self, source='radio', *, code=0, reuse=False):
        video_id = 'existing123' if reuse else 'newradio123'
        target = self.existing if reuse else self.songs / f'New [{video_id}].mp3'
        entry = {'id': video_id, 'title': 'New', 'artist': 'Artist', 'playlistIndex': 1}
        job = server.DownloadJob(f'https://music.youtube.com/watch?v={video_id}', 'New', 'Artist', '', 'song', source=source)

        def create_file(*args, **kwargs):
            target.write_bytes(b'new audio')
            # A concurrently copied unrelated song must not inherit radio origin.
            (self.songs / 'Unrelated [unrelated12].mp3').write_bytes(b'unrelated')
            return SimpleNamespace(stdout=[], wait=lambda: code)

        with patch.object(self.manager, 'inspect_entries', return_value=[entry]), \
             patch.object(self.manager, 'refresh_reused_standalone_metadata', return_value=False), \
             patch.object(server.subprocess, 'Popen', side_effect=create_file), \
             patch.object(server, 'ffprobe_metadata', return_value={'title': 'New', 'artist': 'Artist', 'album': 'Album'}):
            self.manager.run_job(job)
        return target, job

    def test_radio_flags_only_new_files_and_survives_restart_and_rebuild(self):
        target, job = self.download()
        self.assertEqual(job.status, 'succeeded')
        self.assertEqual(job.snapshot()['source'], 'radio')
        self.assertEqual(server.radio_track_filenames(self.songs), {target.name})
        reloaded = server.CatalogIndex(self.songs)
        self.assertTrue(reloaded.track_for_filename(target.name)['isRadioDownload'])
        self.assertFalse(reloaded.track_for_filename(self.existing.name)['isRadioDownload'])
        reloaded.index_path.unlink()
        with patch.object(server, 'ffprobe_metadata', return_value={}):
            reloaded.refresh(force=True)
        record = reloaded.track_for_filename(target.name)
        self.assertTrue(server.track_payload_from_record(record, 'http://localhost')['isRadioDownload'])

    def test_radio_reusing_existing_song_never_marks_it_for_bulk_deletion(self):
        _, job = self.download(reuse=True)
        self.assertEqual(job.status, 'succeeded')
        self.assertEqual(job.reused_files, 1)
        self.assertFalse(server.radio_track_filenames(self.songs))
        self.handler.delete_radio_downloads()
        self.assertTrue(self.existing.exists())
        self.assertEqual(self.handler.write_json.call_args.args[0]['deletedFiles'], 0)

    def test_manual_download_is_not_flagged(self):
        target, job = self.download(source='manual')
        self.assertEqual(job.status, 'succeeded')
        self.assertFalse(self.catalog.track_for_filename(target.name).get('isRadioDownload'))
        self.assertFalse(server.radio_track_filenames(self.songs))

    def test_finished_file_from_failed_radio_job_is_still_flagged(self):
        target, job = self.download(code=1)
        self.assertEqual(job.status, 'failed')
        self.assertIn(target.name, server.radio_track_filenames(self.songs))

    def test_bulk_delete_keeps_other_songs_and_cleans_playlists_and_flags(self):
        target, _ = self.download()
        target.with_suffix('.lrc').write_text('lyrics')
        record = self.catalog.track_for_filename(target.name)
        self.playlists.upsert(str(uuid.uuid4()), {'title': 'Mix', 'trackIDs': [self.existing_record['id'], record['id']]})
        self.handler.write_radio_downloads()
        self.assertEqual(self.handler.write_json.call_args.args[0]['trackIDs'], [record['id']])
        self.handler.delete_radio_downloads()
        self.assertEqual(self.handler.write_json.call_args.args[0]['deletedFiles'], 1)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_suffix('.lrc').exists())
        self.assertTrue(self.existing.exists())
        self.assertTrue((self.songs / 'Unrelated [unrelated12].mp3').exists())
        self.assertEqual(self.playlists.all()[0]['trackIDs'], [self.existing_record['id']])
        self.assertFalse(server.radio_track_filenames(self.songs))
        self.handler.delete_radio_downloads()
        self.assertEqual(self.handler.write_json.call_args.args[0]['deletedFiles'], 0)

    def test_bulk_delete_waits_for_active_download(self):
        target, _ = self.download()
        with patch.object(self.manager, 'active_job', return_value=object()):
            self.handler.delete_radio_downloads()
        self.assertEqual(self.handler.write_json.call_args.kwargs['status'], 409)
        self.assertTrue(target.exists())

    def test_individual_deletion_clears_radio_marker(self):
        target, _ = self.download()
        self.catalog.delete_track_records([self.catalog.track_for_filename(target.name)])
        self.assertFalse(server.radio_track_filenames(self.songs))

    def test_request_validates_source_and_defaults_to_manual(self):
        with patch.object(threading.Thread, 'start'):
            job = self.manager.start({'link': 'https://music.youtube.com/watch?v=x', 'kind': 'song', 'source': 'radio'})
            self.assertEqual(job.source, 'radio')
            self.manager.clear_active(job)
            job = self.manager.start({'link': 'https://music.youtube.com/watch?v=x', 'kind': 'song'})
            self.assertEqual(job.source, 'manual')
        for payload in [{'kind': 'album', 'source': 'radio'}, {'kind': 'song', 'source': 'unknown'}]:
            with self.assertRaises(server.DownloadValidationError):
                self.manager.start({'link': 'https://music.youtube.com/watch?v=x', **payload})

    def keep(self, track_id):
        self.handler.keep_radio_download(urlparse(f'/api/radio-downloads/{track_id}/keep'))

    def test_keep_survives_restart_rebuild_and_bulk_cleanup_without_changing_audio_or_playlists(self):
        target, _ = self.download()
        record = self.catalog.track_for_filename(target.name)
        original_audio = target.read_bytes()
        target.with_suffix('.lrc').write_text('lyrics')
        self.playlists.upsert(str(uuid.uuid4()), {'title': 'Mix', 'trackIDs': [record['id']]})
        self.keep(record['id'])
        self.keep(record['id'])  # Idempotent retry after a lost response.
        self.assertFalse(self.handler.write_json.call_args.args[0]['isRadioDownload'])
        self.assertFalse(server.radio_track_filenames(self.songs))
        self.download()  # Radio reusing a kept file must not flag it again.
        self.assertFalse(server.radio_track_filenames(self.songs))
        reloaded = server.CatalogIndex(self.songs)
        reloaded.index_path.unlink()
        with patch.object(server, 'ffprobe_metadata', return_value={}):
            reloaded.refresh(force=True)
        self.assertFalse(reloaded.track_for_id(record['id'])['isRadioDownload'])
        self.handler.delete_radio_downloads()
        self.assertEqual(target.read_bytes(), original_audio)
        self.assertEqual(target.with_suffix('.lrc').read_text(), 'lyrics')
        self.assertEqual(self.playlists.all()[0]['trackIDs'], [record['id']])
        self.assertEqual(self.handler.write_json.call_args.args[0]['deletedFiles'], 0)

    def test_keep_errors_preserve_flag_and_files(self):
        target, _ = self.download()
        record = self.catalog.track_for_filename(target.name)
        with patch.object(self.manager, 'active_job', return_value=object()):
            self.keep(record['id'])
        self.assertEqual(self.handler.write_json.call_args.kwargs['status'], 409)
        with patch.object(server, 'save_radio_track_filenames', side_effect=OSError('disk full')):
            self.keep(record['id'])
        self.assertEqual(self.handler.write_json.call_args.kwargs['status'], 500)
        self.assertIn(target.name, server.radio_track_filenames(self.songs))
        self.assertTrue(target.exists())
        self.keep('invalid')
        self.assertEqual(self.handler.write_json.call_args.kwargs['status'], 400)
        self.keep(str(uuid.uuid4()))
        self.assertEqual(self.handler.write_json.call_args.kwargs['status'], 404)

    def test_keep_route_and_ordinary_song_are_safe(self):
        self.handler.path = f'/api/radio-downloads/{self.existing_record["id"]}/keep'
        self.handler.do_POST()
        self.assertFalse(self.handler.write_json.call_args.args[0]['isRadioDownload'])
        self.assertTrue(self.existing.exists())

    def test_routes_expose_listing_and_bulk_deletion(self):
        self.handler.path = '/api/radio-downloads'
        self.handler.write_radio_downloads = Mock()
        self.handler.delete_radio_downloads = Mock()
        self.handler.do_GET()
        self.handler.do_DELETE()
        self.handler.write_radio_downloads.assert_called_once()
        self.handler.delete_radio_downloads.assert_called_once()


if __name__ == '__main__':
    unittest.main()
