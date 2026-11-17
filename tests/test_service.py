"""端到端生成测试：用本地假 Emby 服务验证缓存、写回和跳过逻辑。"""
from __future__ import annotations

import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from app import cache, store
from app.jobs import JobManager, JobOptions
from app.render import RenderError
from app.service import generate_one
from app.settings import AppConfig, Server

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "static" / "samples" / "1.jpg"


class FakeEmby(BaseHTTPRequestHandler):
    posts: list = []
    image_gets = 0
    tag = "t1"

    def log_message(self, *_args) -> None:  # 安静
        pass

    def _json(self, obj) -> None:
        import json
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/System/Info/Public":
            return self._json({"ServerName": "Fake", "Version": "10.10.7", "ProductName": "LiMe"})
        if path == "/Library/MediaFolders":
            return self._json({"Items": [
                {"Id": "lib1", "Name": "测试库", "CollectionType": "movies"},
            ]})
        if path == "/Items":
            return self._json({"Items": [
                {"Id": "item1", "Name": "A", "Type": "Movie", "ImageTags": {"Primary": FakeEmby.tag}},
                {"Id": "item2", "Name": "B", "Type": "Movie", "ImageTags": {"Primary": FakeEmby.tag}},
            ]})
        if re.fullmatch(r"/Items/[^/]+/Images/Primary", path):
            FakeEmby.image_gets += 1
            data = SAMPLE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        FakeEmby.posts.append((urlparse(self.path).path, body))
        self.send_response(204)
        self.end_headers()


class ServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeEmby)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self) -> None:
        FakeEmby.posts = []
        FakeEmby.image_gets = 0
        FakeEmby.tag = "t1"
        self.tmp = tempfile.TemporaryDirectory()
        self._state_file = store.STATE_FILE
        self._cache_dir = cache.CACHE_DIR
        store.STATE_FILE = Path(self.tmp.name) / "state.json"
        store._state = None
        cache.CACHE_DIR = Path(self.tmp.name) / "cache"
        self.server = Server(id="fake", url=self.base, api_key="k", name="Fake")

    def tearDown(self) -> None:
        store.STATE_FILE = self._state_file
        store._state = None
        cache.CACHE_DIR = self._cache_dir
        self.tmp.cleanup()

    def _generate(self, apply: bool):
        return generate_one(
            server=self.server,
            library_id="lib1",
            library_name="测试库",
            style="static_4",
            resolution="480p",
            title_zh="测试库",
            title_en="MOVIES",
            params={"style": "static_4", "resolution": "480p"},
            apply=apply,
        )

    @unittest.skipUnless(SAMPLE.is_file(), "缺少样例海报")
    def test_preview_apply_and_skip(self) -> None:
        first = self._generate(apply=False)
        self.assertTrue(first.data.startswith(b"\x89PNG"))
        self.assertFalse(first.skipped)
        self.assertEqual(len(FakeEmby.posts), 0)
        self.assertGreater(FakeEmby.image_gets, 0)

        # 预览不写指纹，第二次 apply 仍然要生成
        second = self._generate(apply=True)
        self.assertFalse(second.skipped)
        self.assertEqual(len(FakeEmby.posts), 1)

        # 参数和海报 tag 都没变 -> 跳过，不再上传
        third = self._generate(apply=True)
        self.assertTrue(third.skipped)
        self.assertEqual(len(FakeEmby.posts), 1)

        # 海报 tag 变了 -> 重新生成
        FakeEmby.tag = "t2"
        fourth = self._generate(apply=True)
        self.assertFalse(fourth.skipped)
        self.assertEqual(len(FakeEmby.posts), 2)

    @unittest.skipUnless(SAMPLE.is_file(), "缺少样例海报")
    def test_cached_posters_are_downloaded_once(self) -> None:
        self._generate(apply=False)
        first_count = FakeEmby.image_gets
        self.assertGreater(first_count, 0)
        self._generate(apply=False)
        self.assertEqual(FakeEmby.image_gets, first_count)

    def test_no_posters_raises(self) -> None:
        FakeEmby.tag = ""
        with self.assertRaises(RenderError):
            self._generate(apply=False)

    @unittest.skipUnless(SAMPLE.is_file(), "缺少样例海报")
    def test_job_runs_all_libraries(self) -> None:
        cfg = AppConfig(
            servers=[self.server],
            defaults={"style": "static_4", "resolution": "480p"},
            schedule={},
            libraries={},
        )
        manager = JobManager()
        manager.start(JobOptions(servers=[self.server], cfg=cfg, apply=True))
        for _ in range(200):
            if not manager.running():
                break
            time.sleep(0.1)
        snap = manager.snapshot() or {}
        self.assertFalse(snap.get("running"))
        self.assertEqual(snap.get("total"), 1)
        self.assertEqual(snap.get("ok"), 1)
        self.assertEqual(snap.get("failed"), 0)
        self.assertEqual(len(FakeEmby.posts), 1)


if __name__ == "__main__":
    unittest.main()
