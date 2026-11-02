#!/usr/bin/env python3
"""An independent, draggable quota companion. Requires the system PyQt5."""
import argparse
import hashlib
import json
import os
import signal
from pathlib import Path
import sys
import tempfile
import threading
import time

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QLockFile
from PyQt5.QtGui import QColor, QPainter, QPen, QCursor
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar, QMenu

from server import Usage, ROOT, number


def window_name(minutes):
    if minutes == 300:
        return "5 小时"
    if minutes == 10080:
        return "每周"
    if not number(minutes):
        return "额度"
    if minutes % 1440 == 0:
        return "{} 天".format(minutes // 1440)
    if minutes % 60 == 0:
        return "{} 小时".format(minutes // 60)
    return "{} 分钟".format(minutes)


def row_text(row):
    value = row.get("remainingPercent")
    return "{:g}%".format(max(0, min(100, value))) if number(value) else "未知"


class Reader(QThread):
    result = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.wake = threading.Event()
        self.stopping = threading.Event()

    def run(self):
        usage = Usage()
        try:
            while not self.stopping.is_set():
                self.wake.clear()
                snapshot = usage.read()
                if not self.stopping.is_set():
                    self.result.emit(snapshot)
                self.wake.wait(30)
        finally:
            usage.backend.close()

    def stop(self):
        self.stopping.set()
        self.wake.set()


class Mascot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(30, 30)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#68d5bd"))
        painter.drawRoundedRect(3, 6, 24, 21, 8, 8)
        painter.drawEllipse(3, 2, 8, 10)
        painter.drawEllipse(19, 2, 8, 10)
        painter.setBrush(QColor("#15362f"))
        painter.drawEllipse(9, 13, 3, 4)
        painter.drawEllipse(18, 13, 3, 4)
        painter.setPen(QPen(QColor("#15362f"), 1.5))
        painter.drawArc(11, 17, 9, 5, 180 * 16, 180 * 16)


class Overlay(QWidget):
    def __init__(self, state_dir, capture=None, preview=False):
        super().__init__()
        self.state_dir = Path(state_dir)
        self.capture = capture
        self.snapshot = None
        self.compact = False
        self.drag_origin = None
        self.preview = preview
        self.closing = False
        self.setWindowTitle("Codex 额度悬浮窗")
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet("""
            QWidget { color: #edf4f5; font-family: 'Noto Sans CJK SC', sans-serif; font-size: 12px; }
            QLabel { background: transparent; }
            QPushButton { border: none; border-radius: 6px; color: #9eaeb3; background: transparent; }
            QPushButton:hover { background: #344147; color: #ffffff; }
            QProgressBar { border: none; background: #354046; border-radius: 2px; }
            QProgressBar::chunk { background: #68d5bd; border-radius: 2px; }
            QMenu { background: #253138; border: 1px solid #45545c; padding: 5px; }
            QMenu::item { padding: 6px 16px; }
            QMenu::item:selected { background: #3c5058; }
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)
        header = QHBoxLayout()
        header.setSpacing(7)
        header.addWidget(Mascot(self))
        self.title = QLabel("Codex 额度")
        self.title.setStyleSheet("font-weight: 600; font-size: 13px;")
        self.title.setAttribute(Qt.WA_TransparentForMouseEvents)
        header.addWidget(self.title, 1)
        self.fold = QPushButton("−")
        self.fold.setAccessibleName("收起或展开额度悬浮窗")
        self.fold.setToolTip("收起 / 展开（也可双击）")
        self.fold.setFixedSize(22, 22)
        self.fold.clicked.connect(self.toggle)
        close = QPushButton("×")
        close.setAccessibleName("关闭额度悬浮窗")
        close.setToolTip("关闭悬浮窗")
        close.setFixedSize(22, 22)
        close.clicked.connect(self.close)
        header.addWidget(self.fold)
        header.addWidget(close)
        layout.addLayout(header)
        self.summary = QLabel("正在读取额度…")
        self.summary.setStyleSheet("color: #a7e1d3; font-size: 12px;")
        self.summary.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.summary.hide()
        layout.addWidget(self.summary)
        self.body = QWidget()
        self.body.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(8)
        layout.addWidget(self.body)
        self.status = QLabel("正在读取 · 可拖动到任意位置")
        self.status.setStyleSheet("color: #93a4ab; font-size: 10px;")
        self.status.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(self.status)
        self.setFixedWidth(278)
        self.reader = None
        self.state = {}
        try:
            self.state = json.loads((self.state_dir / "position.json").read_text())
        except (OSError, ValueError):
            pass
        if self.state.get("compact"):
            self.toggle()
        self.resize_to_content()
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)
        if isinstance(self.state.get("x"), int) and isinstance(self.state.get("y"), int):
            self.move(self.state["x"], self.state["y"])
        self.clamp_position()
        if not preview:
            self.reader = Reader()
            self.reader.result.connect(self.render)
            self.reader.finished.connect(self.finish_close)
            self.reader.start()
        self.stale_timer = QTimer(self)
        self.stale_timer.timeout.connect(self.check_stale)
        self.stale_timer.start(5000)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#435159"), 1))
        painter.setBrush(QColor(31, 41, 47, 247))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 16, 16)

    def resize_to_content(self):
        self.layout().activate()
        self.setFixedHeight(self.sizeHint().height())

    def render(self, snapshot):
        self.snapshot = snapshot
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            item.widget().deleteLater()
        rows = snapshot.get("rows", []) if snapshot.get("ok") else []
        summaries, tips = [], []
        for row in rows:
            name = window_name(row.get("windowMinutes"))
            bucket = row.get("limitName")
            if bucket and bucket != "codex":
                name = bucket + " · " + name
            value = row_text(row)
            summaries.append(name + " " + value)
            reset = row.get("resetsAt")
            when = time.strftime("%m/%d %H:%M", time.localtime(reset)) if number(reset) else "未知"
            tips.append(name + "：剩余 " + value + "；重置 " + when)
            line = QWidget()
            box = QVBoxLayout(line)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(4)
            labels = QHBoxLayout()
            label = QLabel(name)
            label.setWordWrap(True)
            labels.addWidget(label, 1)
            percent = QLabel(value + " 剩余")
            percent.setStyleSheet("font-weight: 600; color: #a7e1d3;")
            labels.addWidget(percent)
            box.addLayout(labels)
            bar = QProgressBar()
            bar.setTextVisible(False)
            bar.setFixedHeight(4)
            bar.setRange(0, 1000)
            remaining = row.get("remainingPercent")
            bar.setValue(round(max(0, min(100, remaining)) * 10) if number(remaining) else 0)
            if number(remaining) and remaining <= 20:
                bar.setStyleSheet("QProgressBar::chunk { background: #e7a45d; }")
            box.addWidget(bar)
            self.body_layout.addWidget(line)
            line.show()
        if not rows:
            error = snapshot.get("error") or "额度暂不可用"
            label = QLabel(error)
            label.setWordWrap(True)
            self.body_layout.addWidget(label)
            label.show()
            summaries = ["额度暂不可用"]
            tips = [error]
        self.summary.setText(" · ".join(summaries))
        self.summary.setWordWrap(True)
        self.setToolTip("\n".join(tips) + "\n本机已登录账号 · 右键可刷新")
        fetched = snapshot.get("fetchedAt", time.time())
        self.status.setText(("示例预览" if self.preview else "更新 " + time.strftime("%H:%M:%S", time.localtime(fetched))) + " · 每 30 秒")
        self.body_layout.invalidate()
        self.body_layout.activate()
        self.layout().invalidate()
        self.resize_to_content()
        self.clamp_position()
        QTimer.singleShot(0, self.resize_to_content)
        if self.capture:
            path, self.capture = self.capture, None
            QTimer.singleShot(200, lambda: self.save_capture(path))
        print(json.dumps({"event": "updated", "ok": snapshot.get("ok"), "rows": rows,
                          "alwaysOnTop": bool(self.windowFlags() & Qt.WindowStaysOnTopHint),
                          "visible": self.isVisible()}, ensure_ascii=False), flush=True)

    def save_capture(self, path):
        self.grab().save(str(path))

    def check_stale(self):
        if self.snapshot and time.time() - self.snapshot.get("fetchedAt", 0) > 90:
            self.status.setText("数据已过期 · 等待下一次更新")

    def toggle(self):
        self.compact = not self.compact
        self.body.setVisible(not self.compact)
        self.summary.setVisible(self.compact)
        self.status.setVisible(not self.compact)
        self.fold.setText("+" if self.compact else "−")
        self.resize_to_content()
        self.save_position()

    def clamp_position(self):
        screens = QApplication.screens()
        screen = next((s.availableGeometry() for s in screens if s.availableGeometry().contains(self.frameGeometry().center())),
                      QApplication.primaryScreen().availableGeometry())
        self.move(max(screen.left(), min(self.x(), screen.right() - self.width() + 1)),
                  max(screen.top(), min(self.y(), screen.bottom() - self.height() + 1)))

    def save_position(self):
        if self.preview:
            return
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            path = self.state_dir / "position.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"x": self.x(), "y": self.y(), "compact": self.compact}))
            temporary.replace(path)
        except OSError:
            pass

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_origin = event.globalPos() - self.frameGeometry().topLeft()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()

    def mouseMoveEvent(self, event):
        if self.drag_origin is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self.drag_origin)
            event.accept()

    def mouseReleaseEvent(self, event):
        self.drag_origin = None
        self.unsetCursor()
        self.clamp_position()
        self.save_position()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.toggle()

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        refresh = menu.addAction("立即刷新")
        fold = menu.addAction("展开" if self.compact else "收起")
        menu.addSeparator()
        close = menu.addAction("退出悬浮窗")
        chosen = menu.exec_(event.globalPos())
        if chosen == refresh and self.reader:
            self.reader.wake.set()
        elif chosen == fold:
            self.toggle()
        elif chosen == close:
            self.close()

    def finish_close(self):
        if self.closing:
            QApplication.quit()

    def closeEvent(self, event):
        self.save_position()
        self.closing = True
        if self.reader and self.reader.isRunning():
            event.ignore()
            self.hide()
            self.reader.stop()
        else:
            event.accept()
            QApplication.quit()


def instance_names():
    user = str(os.getuid()) if hasattr(os, "getuid") else os.environ.get("USERNAME", "local")
    token = hashlib.sha256(user.encode()).hexdigest()[:12]
    return "codex-usage-overlay-" + token, Path(tempfile.gettempdir()) / ("codex-usage-overlay-" + token + ".lock")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, help="Save the first rendered widget image")
    parser.add_argument("--preview", action="store_true", help="Render sample data without connecting to Codex")
    parser.add_argument("--status", action="store_true", help="Check whether the floating window is running")
    parser.add_argument("--quit", action="store_true", help="Close the existing quota window")
    args = parser.parse_args()
    app = QApplication([sys.argv[0]])
    app.setQuitOnLastWindowClosed(False)
    name, lock_path = instance_names()
    lock = None
    if not args.preview:
        socket = QLocalSocket()
        socket.connectToServer(name)
        if socket.waitForConnected(400):
            if not args.status:
                socket.write(b"quit" if args.quit else b"show")
                socket.waitForBytesWritten(400)
            socket.disconnectFromServer()
            print('{"event":"existing-window-shown"}', flush=True)
            return 0
        if args.status or args.quit:
            return 1
        lock = QLockFile(str(lock_path))
        if not lock.tryLock(500):
            print("额度悬浮窗正在启动，请稍后重试。", file=sys.stderr)
            return 1
        QLocalServer.removeServer(name)
    state_dir = os.environ.get("PLUGIN_DATA", str(ROOT / ".local-state"))
    overlay = Overlay(state_dir, args.capture, preview=args.preview)
    server = QLocalServer()
    if not args.preview:
        server.setSocketOptions(QLocalServer.UserAccessOption)
        if not server.listen(name):
            overlay.close()
            return 1
        def accept():
            connection = server.nextPendingConnection()
            def command():
                message = bytes(connection.readAll())
                if message == b"show":
                    overlay.show()
                elif message == b"quit":
                    overlay.close()
            connection.readyRead.connect(command)
            connection.disconnected.connect(connection.deleteLater)
            if connection.bytesAvailable():
                command()
        server.newConnection.connect(accept)
    overlay.show()
    signal.signal(signal.SIGTERM, lambda *_: overlay.close())
    signal.signal(signal.SIGINT, lambda *_: overlay.close())
    print('{"event":"ready"}', flush=True)
    if args.preview:
        overlay.render({"ok": True, "fetchedAt": int(time.time()), "rows": [
            {"limitName": "codex", "windowMinutes": 300, "remainingPercent": 68, "resetsAt": None},
            {"limitName": "codex", "windowMinutes": 10080, "remainingPercent": 42, "resetsAt": None}]})
        QTimer.singleShot(500, app.quit)
    result = app.exec_()
    server.close()
    if lock:
        lock.unlock()
    return result


if __name__ == "__main__":
    sys.exit(main())
