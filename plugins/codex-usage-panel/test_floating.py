"""Run with QT_QPA_PLATFORM=offscreen python3 test_floating.py."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import tempfile
import time
import unittest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QLabel, QProgressBar
from floating import Overlay


class FloatingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.widget = Overlay(self.directory.name, preview=True)
        self.widget.show()

    def tearDown(self):
        self.widget.close()
        self.app.processEvents()
        self.directory.cleanup()

    def render(self, rows, ok=True):
        self.widget.render({"ok": ok, "rows": rows, "error": None if ok else "连接失败",
                            "fetchedAt": int(time.time())})
        self.app.processEvents()

    def test_first_render_has_room_for_both_rows(self):
        empty_height = self.widget.height()
        self.render([{"windowMinutes": 300, "remainingPercent": 0},
                     {"windowMinutes": 10080, "remainingPercent": None}])
        bars = self.widget.body.findChildren(QProgressBar)
        self.assertEqual(len(bars), 2)
        self.assertGreater(self.widget.height(), empty_height + 30)
        for bar in bars:
            self.assertTrue(bar.isVisible())
            self.assertGreater(bar.height(), 0)
            point = bar.mapTo(self.widget, bar.rect().bottomRight())
            self.assertTrue(self.widget.rect().contains(point))
        text = [label.text() for label in self.widget.body.findChildren(QLabel)]
        self.assertIn("0% 剩余", text)
        self.assertIn("未知 剩余", text)

    def test_compact_preserves_data_and_expands(self):
        self.render([{"windowMinutes": 300, "remainingPercent": 65},
                     {"windowMinutes": 10080, "remainingPercent": 40}])
        height = self.widget.height()
        self.widget.toggle()
        self.app.processEvents()
        self.assertFalse(self.widget.body.isVisible())
        self.assertIn("65%", self.widget.summary.text())
        self.assertIn("40%", self.widget.summary.text())
        self.assertLess(self.widget.height(), height)
        self.widget.toggle()
        self.app.processEvents()
        self.assertTrue(self.widget.body.isVisible())

    def test_failed_refresh_replaces_old_data_and_window_stays_on_top(self):
        self.render([{"windowMinutes": 300, "remainingPercent": 65}])
        self.render([], ok=False)
        self.assertNotIn("65%", self.widget.summary.text())
        self.assertIn("连接失败", self.widget.toolTip())
        self.assertTrue(self.widget.windowFlags() & Qt.WindowStaysOnTopHint)
        self.widget.move(-10000, -10000)
        self.widget.clamp_position()
        self.assertGreaterEqual(self.widget.x(), 0)
        self.assertGreaterEqual(self.widget.y(), 0)


if __name__ == "__main__":
    unittest.main()
