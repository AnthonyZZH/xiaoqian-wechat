"""Forge API contract tests: loopback HTTP only; no GPU, WeChat or data/ writes."""
import base64
import io
import json
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from unittest import mock

from PIL import Image

from forge import DEFAULT_MODEL, DEFAULT_PARAMS, DrawingStopped, Forge, start_server


class FakeForge:
    def __init__(self):
        self.requests = []
        self.job_count = 0
        self.job = ''
        self.submitted = threading.Event()
        self.release = threading.Event()
        self.release.set()
        buffer = io.BytesIO()
        Image.new('RGB', (16, 16), 'orange').save(buffer, format='PNG')
        self.image = buffer.getvalue()
        self.data_uri = False
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, data):
                raw = json.dumps(data).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                state.requests.append(('GET', self.path, None))
                route = urlsplit(self.path).path
                if route == '/sdapi/v1/sd-models':
                    self.reply([{'title': DEFAULT_MODEL, 'model_name': 'example-model'}])
                elif route == '/sdapi/v1/progress':
                    self.reply({'progress': 0, 'state': {'job_count': state.job_count, 'job': state.job}})
                else:
                    self.send_error(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                state.requests.append(('POST', self.path, body))
                if self.path == '/sdapi/v1/txt2img':
                    state.submitted.set()
                    if not state.release.wait(5):
                        self.send_error(504)
                        return
                    image = base64.b64encode(state.image).decode('ascii')
                    if state.data_uri:
                        image = 'data:image/png;base64,' + image
                    self.reply({'images': [image], 'info': '{}'})
                elif self.path in ('/sdapi/v1/unload-checkpoint', '/sdapi/v1/reload-checkpoint',
                                   '/sdapi/v1/interrupt', '/sdapi/v1/options'):
                    self.reply({})
                else:
                    self.send_error(404)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01),
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()


class ForgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix='forge-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = FakeForge()
        self.addCleanup(self.fake.close)
        self.client = Forge(self.fake.url)
        self.output = self.root / 'images' / 'job.png'

    def posts(self):
        return [(route, body) for method, route, body in self.fake.requests if method == 'POST']

    def assert_no_global_mutations(self):
        self.assertFalse(any(route in ('/sdapi/v1/interrupt', '/sdapi/v1/options')
                             for route, _ in self.posts()))

    @contextmanager
    def blocked_generation(self):
        """Release and join the actual client worker before HTTP/temp teardown, even on failure."""
        self.fake.release.clear()
        workers = []
        request = self.client.request

        def tracked(route, *args, **kwargs):
            if route == '/sdapi/v1/txt2img':
                workers.append(threading.current_thread())
            return request(route, *args, **kwargs)

        with mock.patch.object(self.client, 'request', side_effect=tracked):
            try:
                yield
            finally:
                self.fake.release.set()
                for worker in workers:
                    worker.join(3)
                    self.assertFalse(worker.is_alive(), 'drawing worker leaked past test cleanup')

    def test_generate_saves_png_and_sends_complete_per_request_settings(self):
        result = self.client.generate('cute cat\n with a cup', DEFAULT_MODEL, self.output,
                                      poll_seconds=0.005)
        self.assertEqual(result, self.output)
        self.assertEqual(result.read_bytes(), self.fake.image)
        self.assertFalse(result.with_suffix('.tmp').exists())
        self.assertEqual(len(self.posts()), 1)
        route, body = self.posts()[0]
        self.assertEqual(route, '/sdapi/v1/txt2img')
        self.assertEqual(body['prompt'], DEFAULT_PARAMS['prefix'] + ' cute cat with a cup')
        self.assertEqual(body['negative_prompt'], DEFAULT_PARAMS['negative'])
        for key in ('steps', 'cfg_scale', 'width', 'height', 'sampler_name', 'scheduler'):
            self.assertEqual(body[key], DEFAULT_PARAMS[key], key)
        self.assertEqual(body['override_settings'], {'sd_model_checkpoint': DEFAULT_MODEL,
                                                     'CLIP_stop_at_last_layers': 2})
        self.assertIs(body['override_settings_restore_afterwards'], True)
        self.assertEqual((body['seed'], body['batch_size'], body['n_iter']), (-1, 1, 1))
        self.assertIs(body['send_images'], True)
        self.assertIs(body['save_images'], False)
        self.assert_no_global_mutations()

    def test_generate_accepts_data_uri_png(self):
        self.fake.data_uri = True
        self.client.generate('cat', DEFAULT_MODEL, self.output, poll_seconds=0.005)
        self.assertEqual(self.output.read_bytes(), self.fake.image)

    def test_missing_model_rejected_without_txt2img(self):
        with self.assertRaises(ValueError):
            self.client.generate('cat', 'missing.safetensors', self.output)
        self.assertEqual(self.posts(), [])
        self.assertFalse(self.fake.submitted.is_set())
        self.assertFalse(self.output.exists())

    def test_cancel_inflight_discards_result_without_interrupting_shared_server(self):
        def cancel():
            return self.fake.submitted.is_set()

        with self.blocked_generation():
            with self.assertRaises(DrawingStopped):
                self.client.generate('cat', DEFAULT_MODEL, self.output, cancel=cancel,
                                     timeout=2, poll_seconds=0.005)
            self.assertTrue(self.fake.submitted.is_set())
            self.assertFalse(self.output.exists())
            self.assert_no_global_mutations()
        # The server has now returned its image; the abandoned worker must still never save it.
        self.assertFalse(self.output.exists())
        self.assertEqual([route for route, _ in self.posts()], ['/sdapi/v1/txt2img'])
        self.assert_no_global_mutations()

    def test_timeout_discards_eventual_result_without_global_interrupt(self):
        with self.blocked_generation():
            with self.assertRaises(TimeoutError):
                self.client.generate('cat', DEFAULT_MODEL, self.output,
                                     timeout=0.1, poll_seconds=0.005)
            self.assertTrue(self.fake.submitted.is_set())
            self.assertFalse(self.output.exists())
        self.assertFalse(self.output.exists())
        self.assert_no_global_mutations()

    def test_busy_job_count_blocks_both_model_controls(self):
        self.fake.job_count = 1
        for action in ('load', 'unload'):
            with self.subTest(action=action), self.assertRaises(RuntimeError):
                getattr(self.client, action)()
        self.assertEqual(self.posts(), [])

    def test_busy_job_name_also_blocks_model_controls(self):
        self.fake.job = 'someone-elses-picture'
        for action in ('load', 'unload'):
            with self.subTest(action=action), self.assertRaises(RuntimeError):
                getattr(self.client, action)()
        self.assertEqual(self.posts(), [])

    def test_idle_load_and_unload_check_progress_before_post(self):
        for action, route in (('load', '/sdapi/v1/reload-checkpoint'),
                              ('unload', '/sdapi/v1/unload-checkpoint')):
            with self.subTest(action=action):
                self.fake.requests.clear()
                getattr(self.client, action)()
                self.assertEqual(len(self.fake.requests), 2)
                method, progress_route, body = self.fake.requests[0]
                self.assertEqual(method, 'GET')
                self.assertEqual(urlsplit(progress_route).path, '/sdapi/v1/progress')
                self.assertEqual(parse_qs(urlsplit(progress_route).query), {'skip_current_image': ['true']})
                self.assertEqual(self.fake.requests[1], ('POST', route, {}))
        self.assert_no_global_mutations()

    def test_start_server_reuses_running_service_without_launching(self):
        with mock.patch('forge.subprocess.Popen') as popen:
            result = start_server(self.fake.url, {}, self.root / 'logs')
        popen.assert_not_called()
        self.assertIn(self.fake.url, result)
        self.assertFalse((self.root / 'logs').exists())

    def test_start_server_rejects_offline_nonlocal_url_without_launching(self):
        # Never perform real external HTTP even to establish that the service is offline.
        with mock.patch.object(Forge, 'request', side_effect=RuntimeError('offline')) as request, \
                mock.patch('forge.subprocess.Popen') as popen:
            with self.assertRaises(ValueError):
                start_server('http://192.0.2.1:7860', {}, self.root / 'logs')
        request.assert_called_once()
        popen.assert_not_called()
        self.assertFalse((self.root / 'logs').exists())

    def test_start_server_launches_local_run_bat_when_offline(self):
        root = self.root / 'forge'
        root.mkdir()
        (root / 'run.bat').touch()
        with mock.patch.object(Forge, 'request', side_effect=[RuntimeError('offline'), []]), \
                mock.patch('forge.subprocess.Popen') as popen:
            popen.return_value.poll.return_value = None
            start_server(self.fake.url, {'forge_root': str(root)}, self.root / 'logs')
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ['cmd', '/c', 'run.bat'])
        self.assertEqual(kwargs['cwd'], str(root))
        self.assertFalse(kwargs.get('shell', False))
        self.assertEqual(Path(kwargs['stdout'].name), self.root / 'logs' / 'forge-server.log')


if __name__ == '__main__':
    unittest.main()
