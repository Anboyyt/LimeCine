"""配置解析与库级覆盖匹配的单元测试。"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import settings


def _write(dirname: str, text: str) -> Path:
    path = Path(dirname) / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class SettingsTest(unittest.TestCase):
    def test_env_expand(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, """
servers:
  lime:
    url: http://192.168.1.10:886
    api_key: ${TEST_KEY}
  art:
    url: ${ART_URL:-http://192.168.1.10:888}
    api_key: ${ART_KEY}
""")
            cfg = settings.load_config(path, env={"TEST_KEY": "abc", "ART_KEY": "def"},
                                       write_migrated=False)
            self.assertEqual([s.id for s in cfg.servers], ["lime", "art"])
            self.assertEqual(cfg.servers[0].api_key, "abc")
            self.assertEqual(cfg.servers[1].url, "http://192.168.1.10:888")

    def test_server_list_form_and_ids(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, """
servers:
  - name: LiMe
    url: 192.168.1.10:886
    api_key: k1
  - url: http://10.0.0.2:8096
""")
            cfg = settings.load_config(path, env={}, write_migrated=False)
            self.assertEqual(cfg.servers[0].id, "lime")
            self.assertEqual(cfg.servers[0].url, "http://192.168.1.10:886")
            self.assertEqual(cfg.servers[1].id, "10-0-0-2-8096")

    def test_resolve_library_priority(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, """
servers:
  lime: {url: "http://x:886", api_key: k}
defaults:
  style: static_1
  resolution: 720p
libraries:
  "type:movies": {style: static_4}
  国产动漫: {title: [国产动漫, ANIME], color: "#111111", style: static_2}
  lime/国产动漫: {title: [国漫, ANIME], style: static_3}
""")
            cfg = settings.load_config(path, env={}, write_migrated=False)
            server = cfg.servers[0]
            plan = settings.resolve_library(cfg, server, {
                "id": "lib-1", "name": "国产动漫", "type": "movies",
            })
            self.assertEqual(plan["params"]["style"], "static_3")
            self.assertEqual(plan["params"]["resolution"], "720p")
            self.assertEqual(plan["title_zh"], "国漫")
            self.assertEqual(plan["title_en"], "ANIME")
            self.assertEqual(plan["color"], "#111111")
            self.assertEqual(plan["matched"], "lime/国产动漫")

    def test_type_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, """
servers:
  lime: {url: "http://x:886", api_key: k}
libraries:
  "type:tvshows": {style: static_3}
""")
            cfg = settings.load_config(path, env={}, write_migrated=False)
            plan = settings.resolve_library(cfg, cfg.servers[0], {
                "id": "s1", "name": "某剧", "type": "tvshows",
            })
            self.assertEqual(plan["params"]["style"], "static_3")
            self.assertEqual(plan["title_en"], "TV SHOWS")

    def test_per_server_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, """
servers:
  lime: {url: "http://x:886", api_key: k, schedule: "04:30"}
  art: {url: "http://y:888", api_key: k}
schedule: {enabled: true, time: "03:00"}
""")
            cfg = settings.load_config(path, env={}, write_migrated=False)
            self.assertEqual(settings.server_schedule(cfg, cfg.servers[0])["time"], "04:30")
            self.assertEqual(settings.server_schedule(cfg, cfg.servers[1])["time"], "03:00")

    def test_legacy_settings_migration(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            legacy = Path(d) / "settings.json"
            legacy.write_text(json.dumps({
                "base_url": "http://192.168.1.10:886",
                "api_key": "secret",
                "style": "static_2",
                "resolution": "720p",
                "schedule_enabled": True,
                "schedule_time": "05:10",
                "title_config": "纪录片:\n  - 纪录片\n  - DOCUMENTARY\n  - '#3A3F44'\n",
            }, ensure_ascii=False), encoding="utf-8")

            with mock.patch.object(settings, "CONFIG_CANDIDATES", []):
                cfg = settings.load_config(
                    None, env={}, legacy_path=legacy, write_migrated=False,
                )
            self.assertEqual(len(cfg.servers), 1)
            self.assertEqual(cfg.servers[0].api_key, "secret")
            self.assertEqual(cfg.defaults["style"], "static_2")
            self.assertTrue(cfg.schedule["enabled"])
            self.assertEqual(cfg.schedule["time"], "05:10")
            self.assertEqual(cfg.libraries["纪录片"]["color"], "#3A3F44")

    def test_missing_explicit_config_errors(self) -> None:
        cfg = settings.load_config(
            Path("/nonexistent/limecine.yaml"),
            env={"LC_URL": "http://10.0.0.9:886", "LC_API_KEY": "k"},
            write_migrated=False,
        )
        # 显式不存在的路径直接报错，不应该偷偷用环境变量
        self.assertTrue(cfg.error)

    def test_auto_reload_when_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, "servers:\n  a: {url: 'http://x:1', api_key: k}\n")
            with mock.patch.object(settings, "CONFIG_CANDIDATES", [path]):
                settings._config = None
                settings._signature = ("", 0.0)
                self.assertEqual(settings.get_config().servers[0].id, "a")

                path.write_text("servers:\n  b: {url: 'http://y:2', api_key: k}\n",
                                encoding="utf-8")
                future = time.time() + 2
                os.utime(path, (future, future))
                self.assertEqual(settings.get_config().servers[0].id, "b")

            settings._config = None
            settings._signature = ("", 0.0)

    def test_view_has_no_key(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, """
servers:
  lime: {url: "http://x:886", api_key: "top-secret"}
""")
            cfg = settings.load_config(path, env={}, write_migrated=False)
            text = json.dumps(settings.view(cfg), ensure_ascii=False)
            self.assertNotIn("top-secret", text)
            self.assertTrue(settings.view(cfg)["servers"][0]["has_key"])


if __name__ == "__main__":
    unittest.main()
