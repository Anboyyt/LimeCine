"""定时任务种子、校验和持久化测试。"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from app import schedules
from app.settings import AppConfig, Server


def make_cfg() -> AppConfig:
    return AppConfig(
        servers=[
            Server(id="lime", url="http://x:886", api_key="k", name="LiMe"),
            Server(id="art", url="http://y:888", api_key="k", name="Art",
                   schedule={"enabled": True, "time": "05:00"}),
            Server(id="off", url="http://z:889", api_key="k", name="Off",
                   schedule={"enabled": False}),
        ],
        defaults={},
        schedule={"enabled": True, "time": "04:00"},
        libraries={},
    )


class ScheduleTest(unittest.TestCase):
    def test_seed_from_config(self) -> None:
        tasks = schedules.seed_from_config(make_cfg())
        by_id = {t["id"]: t for t in tasks}
        self.assertIn("default", by_id)
        self.assertIn("server-art", by_id)
        self.assertNotIn("server-off", by_id)
        # 有显式 schedule 的服务器不进入默认任务
        self.assertEqual(by_id["default"]["servers"], ["lime"])
        self.assertEqual(by_id["default"]["time"], "04:00")
        self.assertEqual(by_id["server-art"]["time"], "05:00")
        self.assertEqual(by_id["server-art"]["servers"], ["art"])

    def test_save_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "schedules.json"
            with mock.patch.object(schedules, "SCHEDULES_FILE", path):
                schedules.save([{
                    "id": "t1", "name": "全量", "enabled": True, "time": "04:30",
                    "servers": ["lime"], "libraries": ["lime/lib1"],
                    "style": "static_4", "resolution": "720p", "force": True,
                }])
                self.assertTrue(path.is_file())
                tasks = schedules.load(make_cfg())
                self.assertEqual(len(tasks), 1)
                task = tasks[0]
                self.assertEqual(task["id"], "t1")
                self.assertEqual(task["time"], "04:30")
                self.assertEqual(task["libraries"], ["lime/lib1"])
                self.assertEqual(task["style"], "static_4")
                self.assertTrue(task["force"])

    def test_validate(self) -> None:
        cfg = make_cfg()
        task = schedules.validate({
            "name": "测试", "time": "9:05", "servers": ["lime"],
            "libraries": ["lime/lib1"], "style": "static_2",
        }, cfg)
        self.assertEqual(task["time"], "09:05")
        self.assertEqual(task["style"], "static_2")

        with self.assertRaises(ValueError):
            schedules.validate({"time": "25:00"}, cfg)
        with self.assertRaises(ValueError):
            schedules.validate({"time": "04:00", "style": "nope"}, cfg)
        with self.assertRaises(ValueError):
            schedules.validate({"time": "04:00", "servers": ["missing"]}, cfg)

    def test_next_run(self) -> None:
        now = datetime(2026, 11, 17, 15, 0)
        self.assertEqual(schedules.next_run({"time": "16:00"}, now), "今天 16:00")
        self.assertEqual(schedules.next_run({"time": "04:00"}, now), "明天 04:00")

    def test_task_servers(self) -> None:
        cfg = make_cfg()
        self.assertEqual(
            [s.id for s in schedules.task_servers(cfg, {"servers": []})],
            ["lime", "art", "off"],
        )
        self.assertEqual(
            [s.id for s in schedules.task_servers(cfg, {"servers": ["art"]})],
            ["art"],
        )


if __name__ == "__main__":
    unittest.main()
