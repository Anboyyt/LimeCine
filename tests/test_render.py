"""渲染冒烟测试（只用静态样式，不依赖 ffmpeg）。"""
from __future__ import annotations

import unittest
from pathlib import Path

from app.config import EN_FONT, ZH_FONT
from app.render import render_cover

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = sorted((ROOT / "static" / "samples").glob("*.jpg"))


@unittest.skipUnless(SAMPLES, "缺少 static/samples")
@unittest.skipUnless(Path(ZH_FONT).is_file() and Path(EN_FONT).is_file(), "缺少字体")
class RenderTest(unittest.TestCase):
    def test_static_4_png(self) -> None:
        data, ext = render_cover(
            "static_4",
            [str(SAMPLES[0])],
            "测试库",
            "MOVIES",
            zh_font=ZH_FONT,
            en_font=EN_FONT,
            resolution="480p",
            zh_font_size=170,
            en_font_size=80,
            blur_size=50,
            color_ratio=0.4,
            bg_mode="auto",
        )
        self.assertEqual(ext, ".png")
        self.assertTrue(data.startswith(b"\x89PNG"), data[:8])

    def test_unknown_style(self) -> None:
        with self.assertRaises(Exception):
            render_cover(
                "nope", [str(SAMPLES[0])], "x", "",
                zh_font=ZH_FONT, en_font=EN_FONT, resolution="480p",
            )


if __name__ == "__main__":
    unittest.main()
