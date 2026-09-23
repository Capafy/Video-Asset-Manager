"""Regression coverage for server read concurrency and response optimizations."""
from __future__ import annotations

import gzip
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / 'scripts'))
spec = importlib.util.spec_from_file_location('vam_latency_server', SKILL / 'assets/webapp/server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ServerLatencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vam-http-test-')
        self.root = Path(self.temp.name)
        self.patch_root = mock.patch.object(server, 'ROOT', str(self.root))
        self.patch_root.start()
        self.patch_sync = mock.patch.object(server, 'schedule_task_sync')
        self.sync_calls = self.patch_sync.start()
        server._PROJECT_SIZE_CACHE.clear()
        self.document = server.empty_project('sample')
        server.write_json_atomic(server.project_file('sample'), self.document)
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.http.server_address[1])

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        self.patch_sync.stop()
        self.patch_root.stop()
        self.temp.cleanup()

    def request(self, path, payload=None, headers=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.url + path, data=data,
                                         headers=headers or {'Content-Type': 'application/json'})
        try:
            response = urllib.request.urlopen(request, timeout=4)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            if response.headers.get('Content-Encoding') == 'gzip':
                raw = gzip.decompress(raw)
            return response.status, json.loads(raw)

    def test_slow_response_does_not_hold_project_lock(self):
        for path in ('/api/project/sample', '/api/project/sample/timeline'):
            with self.subTest(path=path):
                entered, release, acquired = threading.Event(), threading.Event(), threading.Event()
                original = server.Handler._json
                errors = []

                def delayed(handler, *args, **kwargs):
                    entered.set()
                    release.wait(3)
                    return original(handler, *args, **kwargs)

                def fetch():
                    try:
                        self.assertEqual(self.request(path)[0], 200)
                    except Exception as error:
                        errors.append(error)

                def acquire():
                    with server.LOCK:
                        acquired.set()

                with mock.patch.object(server.Handler, '_json', delayed):
                    request_thread = threading.Thread(target=fetch)
                    request_thread.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        lock_thread = threading.Thread(target=acquire)
                        lock_thread.start()
                        self.assertTrue(acquired.wait(1), 'socket response held the project lock')
                    finally:
                        release.set()
                        request_thread.join(4)
                        lock_thread.join(4)
                self.assertEqual(errors, [])

    def test_commit_conflict_undo_redo_and_external_edit(self):
        path = '/api/project/sample/timeline'
        status, before = self.request(path)
        self.assertEqual(status, 200)
        timeline = before['timeline']
        timeline['canvas']['background'] = '#123456'
        payload = {'base_rev': before['rev'], 'operations': [{'op': 'replace_timeline', 'timeline': timeline}]}
        status, saved = self.request(path + '/commit', payload)
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved['rev'], before['rev'] + 1)
        self.assertEqual(self.request(path + '/commit', payload)[0], 409)
        status, undone = self.request(path + '/commit', {'base_rev': saved['rev'], 'operations': [{'op': 'undo'}]})
        self.assertEqual(status, 200, undone)
        self.assertNotEqual(undone['timeline']['canvas']['background'], '#123456')
        status, redone = self.request(path + '/commit', {'base_rev': undone['rev'], 'operations': [{'op': 'redo'}]})
        self.assertEqual(status, 200, redone)
        self.assertEqual(redone['timeline']['canvas']['background'], '#123456')
        document = server.read_json(server.project_file('sample'))
        document['title'] = 'External update'
        document['rev'] += 1
        server.write_json_atomic(server.project_file('sample'), document)
        status, fresh = self.request('/api/project/sample')
        self.assertEqual(status, 200)
        self.assertEqual(fresh['project']['title'], 'External update')
        self.assertEqual(fresh['changes']['project_rev'], document['rev'])
        self.assertNotEqual(fresh['changes']['token'], before['changes']['token'])
        self.assertFalse(any(call.kwargs.get('force') for call in self.sync_calls.mock_calls))

    def test_privacy_and_gzip_keep_the_response_contract(self):
        payload = {'project': {'title': 'Example', 'token': 'f' * 24,
                              'records': [{'name': 'Clip', 'url': 'https://example.test/private',
                                           'path': 'clips/c1/v1.mp4'}] * 30}}
        clean = server.sanitize_public(payload)
        self.assertEqual(clean['project']['records'], [{'name': 'Clip', 'path': 'clips/c1/v1.mp4'}] * 30)
        self.assertEqual(clean, server.sanitize_public(clean))
        with mock.patch.object(server, 'workspace_library', return_value=payload):
            plain = self.request('/api/library')
            compressed = self.request('/api/library', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(plain, compressed)
        self.assertEqual(plain, (200, clean))

    def test_alias_normalization_and_lightweight_index(self):
        self.document['asset']['media'] = [{'id': 'as_a', 'file': 'assets/uploads/a.mp4'}]
        self.document['assets'] = [
            {'id': 'as_alias', 'file': 'assets/uploads/a.mp4'},
            {'id': 'as_b', 'file': 'assets/uploads/b.mp4'},
            {'id': 'as_b_alias', 'file': 'assets/uploads/b.mp4'}]
        with mock.patch.object(server, '_hydrate_clip_media_durations') as hydrate:
            normalized = server.normalize_project(self.document, 'sample')
            hydrate.assert_called_once()
        self.assertEqual([item['id'] for item in normalized['assets']], ['as_a', 'as_b'])
        self.assertEqual(normalized['assets'], normalized['asset']['media'])
        with mock.patch.object(server, '_hydrate_clip_media_durations', side_effect=AssertionError('index probed media')):
            server.sync_index()
        self.assertEqual(server.read_index_cached()['projects'][0]['slug'], 'sample')

    def test_size_recounts_saved_files_and_excludes_staging(self):
        directory = self.root / 'sample'
        (directory / 'assets').mkdir()
        (directory / 'assets' / 'data.txt').write_bytes(b'public')
        (directory / 'staging').mkdir()
        (directory / 'staging' / 'private.txt').write_bytes(b'excluded')
        self.assertEqual(server._project_size_cached('sample'),
                         (directory / 'project.json').stat().st_size + len(b'public'))

        self.document['title'] = 'A longer changed project title'
        server.write_json_atomic(server.project_file('sample'), self.document)
        self.assertEqual(server._project_size_cached('sample'),
                         (directory / 'project.json').stat().st_size + len(b'public'))
        try:
            (directory / 'assets' / 'link.txt').symlink_to(directory / 'project.json')
        except OSError:
            return  # Symlink privileges are optional on Windows.
        server._PROJECT_SIZE_CACHE.clear()
        self.assertEqual(server._project_size_cached('sample'),
                         (directory / 'project.json').stat().st_size + len(b'public'))

    def test_timeline_index_updates_only_changed_project_and_stays_current(self):
        other = server.empty_project('other', 'Other project')
        server.write_json_atomic(server.project_file('other'), other)
        server.sync_index()
        before = server.read_index_cached()
        previous_other = next(row for row in before['projects'] if row['slug'] == 'other')
        path = '/api/project/sample/timeline/commit'
        with mock.patch.object(server, '_project_size_cached', wraps=server._project_size_cached) as sizes:
            status, saved = self.request(path, {'base_rev': 0, 'operations': [
                {'op': 'set_canvas', 'canvas': {'background': '#123456'}}]})
            self.assertEqual(status, 200, saved)
            self.assertEqual([call.args[0] for call in sizes.call_args_list], ['sample'])
            self.sync_calls.assert_not_called()
            status, index = self.request('/api/projects')
            self.assertEqual(status, 200)
            self.assertEqual([call.args[0] for call in sizes.call_args_list], ['sample'])
        self.assertEqual(next(row for row in index['projects'] if row['slug'] == 'other'), previous_other)
        self.assertNotEqual(server.index_change_token(before), server.index_change_token(index))
        sample = next(row for row in index['projects'] if row['slug'] == 'sample')
        expected_size = sum(path.stat().st_size for path in (self.root / 'sample').rglob('*') if path.is_file())
        self.assertEqual(sample['size_bytes'], expected_size)

    def test_incremental_index_reconciles_external_changes_and_new_projects(self):
        server.sync_index()
        other = server.empty_project('other', 'New delivery')
        server.write_json_atomic(server.project_file('other'), other)
        for n in range(2):
            status, saved = self.request('/api/project/sample/timeline/commit', {
                'base_rev': n, 'operations': [{'op': 'set_canvas', 'canvas': {'background': '#123456'}}]})
            self.assertEqual(status, 200, saved)
            index = server.read_index_cached()
            self.assertEqual(next(row for row in index['projects'] if row['slug'] == 'other')['title'], other['title'])
            other['title'] = 'Updated external project title'
            other['rev'] += 1
            server.write_json_atomic(server.project_file('other'), other)

    def test_external_edit_during_index_write_remains_detectable(self):
        other = server.empty_project('other', 'Original')
        server.write_json_atomic(server.project_file('other'), other)
        original_write = server.write_json_atomic
        changed = False

        def racing_write(path, value):
            nonlocal changed
            if Path(path).name == 'index.json' and not changed:
                changed = True
                other['title'] = 'Concurrent external update'
                other['rev'] += 1
                original_write(server.project_file('other'), other)
            return original_write(path, value)

        with mock.patch.object(server, 'write_json_atomic', racing_write):
            server.read_index_cached(force=True)
        index = server.read_index_cached()
        self.assertEqual(next(row for row in index['projects'] if row['slug'] == 'other')['title'], other['title'])

    def test_keepalive_cross_track_move_undo_redo_and_revision_conflict(self):
        clip = {'id': 'clip_a', 'status': 'delivered', 'current': 1, 'duration': 4,
                'versions': [{'v': 1, 'file': 'clips/clip_a/v1.mp4', 'duration': 4}]}
        self.document['asset']['clips'] = [clip]
        self.document['clips'] = [clip]
        self.document['assembly']['order'] = ['clip_a']
        server.write_json_atomic(server.project_file('sample'), self.document)
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_address[1], timeout=3)
        sockets = []

        def exchange(path, payload=None):
            connection.request('POST' if payload is not None else 'GET', path,
                               body=json.dumps(payload) if payload is not None else None,
                               headers={'Content-Type': 'application/json', 'Accept-Encoding': 'gzip'})
            response = connection.getresponse()
            raw = response.read()
            self.assertEqual(response.version, 11)
            self.assertIsNotNone(connection.sock)
            sockets.append(connection.sock.getsockname())
            if response.getheader('Content-Encoding') == 'gzip':
                raw = gzip.decompress(raw)
            return response.status, json.loads(raw)

        try:
            path = '/api/project/sample/timeline'
            status, current = exchange(path)
            item_id = current['timeline']['tracks'][0]['clips'][0]['id']
            for n in range(6):
                destination = 'video-overlay' if n % 2 == 0 else 'video-main'
                rev = current['rev']
                status, current = exchange(path + '/commit', {'base_rev': rev, 'operations': [
                    {'op': 'move', 'item_id': item_id, 'track_id': destination, 'start': 0}]})
                self.assertEqual(status, 200, current)
                self.assertEqual(current['rev'], rev + 1)
                self.assertEqual([track['id'] for track in current['timeline']['tracks']
                                  if any(item['id'] == item_id for item in track.get('clips', []))], [destination])
            last_rev = current['rev']
            status, conflict = exchange(path + '/commit', {'base_rev': 0, 'operations': [{'op': 'undo'}]})
            self.assertEqual(status, 409)
            self.assertEqual(conflict['rev'], last_rev)
            for operation, destination in (('undo', 'video-overlay'), ('redo', 'video-main')):
                status, current = exchange(path + '/commit', {'base_rev': current['rev'], 'operations': [{'op': operation}]})
                self.assertEqual(status, 200, current)
                self.assertEqual([track['id'] for track in current['timeline']['tracks'] if track.get('clips')], [destination])
            self.assertEqual(len(set(sockets)), 1)
            self.assertEqual(server.read_json(server.project_file('sample'))['rev'], current['rev'])
        finally:
            connection.close()

    def test_keepalive_download_head_range_and_following_api_response(self):
        directory = self.root / 'sample' / 'media'
        directory.mkdir()
        (directory / 'sample.mp4').write_bytes(b'0123456789')
        (directory / 'empty.mp4').write_bytes(b'')
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_address[1], timeout=3)
        route = '/api/project/sample/file/media/sample.mp4'
        try:
            for method, path, headers, status, expected in (
                ('HEAD', route, {}, 200, b''),
                ('GET', route, {'Range': 'bytes=0-999'}, 206, b'0123456789'),
                ('GET', route, {'Range': 'bytes=2-4'}, 206, b'234'),
                ('GET', route, {'Range': 'bytes=999-'}, 416, b''),
                ('GET', '/sample/media/sample.mp4', {}, 200, b'0123456789'),
                ('GET', '/api/project/sample/file/media/empty.mp4', {}, 200, b''),
                ('HEAD', '/api/project/sample/file/media/missing.mp4', {}, 404, b''),
            ):
                connection.request(method, path, headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, status)
                self.assertEqual(response.read(), expected)
                if method != 'HEAD':
                    self.assertEqual(int(response.getheader('Content-Length')), len(expected))
                connection.request('GET', '/api/project/sample/timeline')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(json.loads(response.read())['ok'])
                self.assertIsNone(response.getheader('Accept-Ranges'))
        finally:
            connection.close()

    def test_unread_post_body_closes_connection_without_poisoning_next_request(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_address[1], timeout=3)
        try:
            for path, headers, expected in (
                ('/api/no-such-action', {}, 404),
                ('/api/project/sample/timeline/commit', {'Content-Length': str(2 * 1024 * 1024 + 1)}, 400),
                ('/api/sync', {}, 200),
            ):
                connection.request('POST', path, body=b'{}', headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                self.assertEqual(response.getheader('Connection'), 'close')
                response.read()
                connection.request('GET', '/api/project/sample/timeline')
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(json.loads(response.read())['ok'])
        finally:
            connection.close()
if __name__ == '__main__':
    unittest.main()
