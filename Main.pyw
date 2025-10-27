import sys
import os
import subprocess
import json
import logging
import numpy as np
import re
import tempfile
from PIL import Image
from collections import deque
from typing import Optional, Tuple
from enum import Enum
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLabel, QPushButton, QComboBox,
                             QSpinBox, QTextEdit, QGroupBox,
                             QFileDialog, QProgressBar, QMessageBox, QFrame)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread, QSize, QProcess, QObject, QRect
from PyQt6.QtGui import QPixmap, QImage, QPainter, QPen, QColor

from qfluentwidgets import (PrimaryPushButton, ComboBox, SpinBox,
                            ProgressBar, TextEdit, TitleLabel,
                            BodyLabel, CaptionLabel, StrongBodyLabel)
from qfluentwidgets import setTheme, Theme
HAS_FLUENT = True

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

API_BASE_URL = "https://paintboard.luogu.me"

PROGRESS_PATTERN = re.compile(
    r".*进度:\s*(\d+)/(\d+)\s*\((\d+(?:\.\d+)?)%\)\s*- 修复任务:\s*(\d+)\s*- 活动账户:\s*(\d+)/(\d+)"
)

class PaintMode(Enum):
    LINE_SCAN = "扫描线"
    RANDOM_DOT = "多线程随机撒点"

    def to_int(self) -> int:
        return {PaintMode.LINE_SCAN: 0, PaintMode.RANDOM_DOT: 1}[self]

class ConsoleRunner(QThread):
    output_received = pyqtSignal(str)
    finished = pyqtSignal(int)

    def __init__(self, cmd: list, temp_file: Optional[str] = None):
        super().__init__()
        self.cmd = cmd
        self.temp_file = temp_file  # 用于后续清理
        self.process = None

    def run(self):
        logger.info(f"Running command: {' '.join(self.cmd)}")
        self.process = subprocess.Popen(
            self.cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        try:
            for line in iter(self.process.stdout.readline, ''):
                if line:
                    self.output_received.emit(line.rstrip())
            self.process.stdout.close()
            exit_code = self.process.wait()
            self.finished.emit(exit_code)
        except Exception as e:
            self.output_received.emit(f"[ERROR] Subprocess failed: {e}")
            self.finished.emit(-1)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def cleanup_temp(self):
        if self.temp_file and os.path.exists(self.temp_file):
            try:
                os.remove(self.temp_file)
            except Exception as e:
                logger.warning(f"Failed to remove temp file {self.temp_file}: {e}")

class CanvasWidget(QWidget):
    image_moved = pyqtSignal(int, int)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(500, 300)
        self.board_img = None
        self.user_img_original = None  # PIL Image, 原始
        self.user_img_display = None   # QPixmap, 用于显示
        self.offset_x = 0
        self.offset_y = 0
        self.user_scale = 1.0
        self.scale = 0.5
        self.board_data = None

        self.dragging = False
        self.drag_start_pos = None
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_board_image(self, img_data: np.ndarray):
        if img_data is not None:
            try:
                self.board_data = img_data
                h, w = img_data.shape[:2]
                img_data_contiguous = np.ascontiguousarray(img_data)
                q_img = QImage(img_data_contiguous.data, w, h, 3 * w, QImage.Format.Format_RGB888)
                self.board_img = QPixmap.fromImage(q_img)
                self.update()
            except Exception as e:
                logger.error(f"Error setting board image: {e}")

    def set_user_image(self, img: Image.Image, ox: int, oy: int, scale: float = 1.0):
        if img:
            self.user_img_original = img.copy()
            self.user_scale = max(0.1, min(5.0, scale))
            # 限制 offset 在有效范围内
            img_w, img_h = img.size
            scaled_w = int(img_w * self.user_scale)
            scaled_h = int(img_h * self.user_scale)
            max_x = max(0, 1000 - scaled_w)
            max_y = max(0, 600 - scaled_h)
            self.offset_x = max(0, min(ox, max_x))
            self.offset_y = max(0, min(oy, max_y))
            self._update_user_display()
            self.update()

    def _update_user_display(self):
        if self.user_img_original is None:
            self.user_img_display = None
            return
        w0, h0 = self.user_img_original.size
        new_w = int(w0 * self.user_scale)
        new_h = int(h0 * self.user_scale)
        if new_w < 1 or new_h < 1:
            self.user_img_display = None
            return
        resized_img = self.user_img_original.resize((new_w, new_h), Image.Resampling.LANCZOS)
        if resized_img.mode != 'RGB':
            resized_img = resized_img.convert('RGB')
        img_array = np.array(resized_img)
        q_img = QImage(img_array.tobytes(), new_w, new_h, 3 * new_w, QImage.Format.Format_RGB888)
        self.user_img_display = QPixmap.fromImage(q_img)

    def _get_board_rect(self):
        aw, ah = self.width(), self.height()
        sx, sy = aw / 1000.0, ah / 600.0
        self.scale = min(sx, sy, 1.0)
        bw, bh = 1000 * self.scale, 600 * self.scale
        bx, by = (aw - bw) / 2, (ah - bh) / 2
        return bx, by, bw, bh

    def _get_user_image_rect(self, board_x, board_y):
        if not self.user_img_display:
            return None
        uw = self.user_img_display.width() * self.scale
        uh = self.user_img_display.height() * self.scale
        scaled = self.user_img_display.scaled(
            int(uw), int(uh),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        actual_w, actual_h = scaled.width(), scaled.height()
        ux = board_x + self.offset_x * self.scale - (actual_w - uw) / 2
        uy = board_y + self.offset_y * self.scale - (actual_h - uh) / 2
        return QRect(int(ux), int(uy), actual_w, actual_h)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor(240, 240, 240))

        board_x, board_y, board_width, board_height = self._get_board_rect()
        painter.fillRect(int(board_x), int(board_y), int(board_width), int(board_height), Qt.GlobalColor.white)

        if self.board_img:
            scaled_board = self.board_img.scaled(
                int(board_width), int(board_height),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            dx = (board_width - scaled_board.width()) / 2
            dy = (board_height - scaled_board.height()) / 2
            painter.drawPixmap(int(board_x + dx), int(board_y + dy), scaled_board)

        if self.user_img_display:
            uw = self.user_img_display.width() * self.scale
            uh = self.user_img_display.height() * self.scale
            scaled_user = self.user_img_display.scaled(
                int(uw), int(uh),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            actual_w, actual_h = scaled_user.width(), scaled_user.height()
            ux = board_x + self.offset_x * self.scale - (actual_w - uw) / 2
            uy = board_y + self.offset_y * self.scale - (actual_h - uh) / 2

            painter.setOpacity(0.7)
            painter.drawPixmap(int(ux), int(uy), scaled_user)
            painter.setOpacity(1.0)
            painter.setPen(QPen(QColor(255, 0, 0), 2))
            painter.drawRect(int(ux), int(uy), actual_w, actual_h)

        # Grid
        painter.setPen(QPen(QColor(200, 200, 200), 1))
        for x in range(0, 1001, 200):
            line_x = board_x + x * self.scale
            painter.drawLine(int(line_x), int(board_y), int(line_x), int(board_y + board_height))
        for y in range(0, 601, 200):
            line_y = board_y + y * self.scale
            painter.drawLine(int(board_x), int(line_y), int(board_x + board_width), int(line_y))

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        self.scale *= 1.1 if delta > 0 else 0.9
        self.scale = max(0.1, min(2.0, self.scale))
        self.update()

    def mousePressEvent(self, event):
        pos = event.pos()
        board_x, board_y, _, _ = self._get_board_rect()
        user_rect = self._get_user_image_rect(board_x, board_y)

        if user_rect and user_rect.contains(pos):
            self.dragging = True
            self.drag_start_pos = pos
            self.orig_offset = (self.offset_x, self.offset_y)
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.dragging and self.drag_start_pos:
            pos = event.pos()
            board_x, board_y, _, _ = self._get_board_rect()
            dx = (pos.x() - self.drag_start_pos.x()) / self.scale
            dy = (pos.y() - self.drag_start_pos.y()) / self.scale
            new_ox = int(self.orig_offset[0] + dx)
            new_oy = int(self.orig_offset[1] + dy)

            # ✅ 正确计算边界：使用原始图像尺寸和当前缩放
            if self.user_img_original:
                orig_w, orig_h = self.user_img_original.size
                scaled_w = int(orig_w * self.user_scale)
                scaled_h = int(orig_h * self.user_scale)
                max_x = max(0, 1000 - scaled_w)
                max_y = max(0, 600 - scaled_h)
                new_ox = max(0, min(new_ox, max_x))
                new_oy = max(0, min(new_oy, max_y))

            self.offset_x = new_ox
            self.offset_y = new_oy
            self.image_moved.emit(new_ox, new_oy)
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        self.dragging = False
        self.drag_start_pos = None
        event.accept()


class BoardMonitor(QThread):
    board_updated = pyqtSignal(np.ndarray)

    def __init__(self):
        super().__init__()
        self._running = False
        self.update_interval = 1  # seconds

    def run(self):
        self._running = True
        import requests
        while self._running:
            try:
                # 使用较短的连接和读取 timeout
                response = requests.get(
                    f"{API_BASE_URL}/api/paintboard/getboard",
                    timeout=(3.0, 5.0)  # (connect, read)
                )
                if response.status_code == 200:
                    data = response.content
                    if len(data) == 1000 * 600 * 3:
                        board = np.frombuffer(data, dtype=np.uint8).reshape((600, 1000, 3))
                        self.board_updated.emit(board)
            except Exception as e:
                logger.error(f"Board monitor error: {e}")

            # 将长 sleep 拆分为小片段，以便快速响应 stop()
            for _ in range(self.update_interval * 10):
                if not self._running:
                    return
                self.msleep(100)  # 每 100ms 检查一次

    def stop(self):
        self._running = False
        # 不要在这里 wait()！


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GenGen Painter")
        self.setMinimumSize(1200, 800)

        self.current_image_path = None
        self.offset_x = 0
        self.offset_y = 0
        self.user_image_scale = 1.0
        self.console_runner = None
        self.board_monitor = BoardMonitor()

        self.setup_ui()
        self.setup_connections()
        self.board_monitor.start()

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QHBoxLayout(central_widget)

        left_panel = QWidget()
        left_panel.setMaximumWidth(350)
        left_layout = QVBoxLayout(left_panel)

        img_group = QGroupBox("图像设置")
        img_layout = QVBoxLayout(img_group)
        self.btn_load = PrimaryPushButton("上传图片")
        img_layout.addWidget(self.btn_load)

        img_layout.addWidget(StrongBodyLabel("位置调整:"))
        pos_layout = QHBoxLayout()
        pos_layout.addWidget(BodyLabel("X:"))
        self.spin_x = SpinBox()
        self.spin_x.setRange(0, 1000)
        pos_layout.addWidget(self.spin_x)
        pos_layout.addWidget(BodyLabel("Y:"))
        self.spin_y = SpinBox()
        self.spin_y.setRange(0, 600)
        pos_layout.addWidget(self.spin_y)
        img_layout.addLayout(pos_layout)

        scale_layout = QHBoxLayout()
        scale_layout.addWidget(BodyLabel("缩放:"))
        self.spin_scale = SpinBox()
        self.spin_scale.setRange(10, 500)   # 0.1 ~ 5.0
        self.spin_scale.setValue(100)       # 1.0
        self.spin_scale.setSingleStep(10)   # 0.1 步长
        scale_layout.addWidget(self.spin_scale)
        img_layout.addLayout(scale_layout)

        self.btn_refresh = PrimaryPushButton("手动刷新画板")
        img_layout.addWidget(self.btn_refresh)
        left_layout.addWidget(img_group)

        paint_group = QGroupBox("绘画设置")
        paint_layout = QVBoxLayout(paint_group)
        paint_layout.addWidget(StrongBodyLabel("绘画模式:"))
        self.combo_mode = ComboBox()
        self.combo_mode.addItems([mode.value for mode in PaintMode])
        paint_layout.addWidget(self.combo_mode)
        self.btn_start = PrimaryPushButton("开始绘画")
        self.btn_stop = PrimaryPushButton("停止绘画")
        self.btn_stop.setEnabled(False)
        btn_layout = QHBoxLayout()
        btn_layout.addWidget(self.btn_start)
        btn_layout.addWidget(self.btn_stop)
        paint_layout.addLayout(btn_layout)
        left_layout.addWidget(paint_group)

        progress_group = QGroupBox("进度")
        progress_layout = QVBoxLayout(progress_group)
        self.label_status = BodyLabel("就绪")
        self.progress_bar = ProgressBar()
        self.progress_bar.setRange(0, 100)
        progress_layout.addWidget(self.label_status)
        progress_layout.addWidget(self.progress_bar)
        left_layout.addWidget(progress_group)

        info_group = QGroupBox("说明")
        info_layout = QVBoxLayout(info_group)
        info_text = QLabel(
            "• 上传图片后可拖动调整位置\n"
            "• 缩放比例通过输入框设置（0.1~5.0）\n"
            "• 点击“开始绘画”将调用:\n"
            "  python Console.py [缩放后图片] [X] [Y] [模式]\n"
            "• 模式: 0=逐行, 1=随机\n"
            "• 画板每秒自动刷新"
        )
        info_text.setWordWrap(True)
        info_layout.addWidget(info_text)
        left_layout.addWidget(info_group)

        log_group = QGroupBox("日志输出")
        log_layout = QVBoxLayout(log_group)
        self.text_log = TextEdit()
        self.text_log.setReadOnly(True)
        log_layout.addWidget(self.text_log)
        left_layout.addWidget(log_group)
        left_layout.addStretch()

        self.canvas = CanvasWidget()
        layout.addWidget(left_panel)
        layout.addWidget(self.canvas, 1)

    def setup_connections(self):
        self.btn_load.clicked.connect(self.load_image)
        self.btn_start.clicked.connect(self.start_painting)
        self.btn_stop.clicked.connect(self.stop_painting)
        self.btn_refresh.clicked.connect(self.refresh_board)
        self.spin_x.valueChanged.connect(self.update_offset_from_spin)
        self.spin_y.valueChanged.connect(self.update_offset_from_spin)
        self.spin_scale.valueChanged.connect(self.update_scale_from_spin)
        self.board_monitor.board_updated.connect(self.canvas.set_board_image)
        self.canvas.image_moved.connect(self.on_image_moved)

    def on_image_moved(self, ox, oy):
        self.offset_x = ox
        self.offset_y = oy
        self.spin_x.setValue(ox)
        self.spin_y.setValue(oy)

    def load_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "Images (*.png *.jpg *.jpeg *.bmp)"
        )
        if path:
            self.current_image_path = path
            try:
                img = Image.open(path).convert('RGB')
                self.user_image_scale = 1.0
                self.spin_scale.setValue(100)
                self.canvas.set_user_image(img, 0, 0, 1.0)  # 初始位置设为 (0,0)
                w, h = img.size
                self.log_info(f"加载图片: {w}x{h}")
            except Exception as e:
                self.log_error(f"加载图片失败: {e}")

    def refresh_board(self):
        self.log_info("手动刷新画板...")
        import requests
        try:
            response = requests.get(f"{API_BASE_URL}/api/paintboard/getboard", timeout=10)
            if response.status_code == 200:
                data = response.content
                if len(data) == 1000 * 600 * 3:
                    board = np.frombuffer(data, dtype=np.uint8).reshape((600, 1000, 3))
                    self.canvas.set_board_image(board)
                    self.log_info("画板刷新成功")
                else:
                    self.log_error("画板数据大小错误")
            else:
                self.log_error(f"HTTP {response.status_code}")
        except Exception as e:
            self.log_error(f"刷新失败: {e}")

    def update_offset_from_spin(self):
        self.offset_x = self.spin_x.value()
        self.offset_y = self.spin_y.value()
        if self.current_image_path:
            try:
                img = Image.open(self.current_image_path).convert('RGB')
                self.canvas.set_user_image(img, self.offset_x, self.offset_y, self.user_image_scale)
            except Exception as e:
                self.log_error(f"更新偏移失败: {e}")

    def update_scale_from_spin(self):
        scale = self.spin_scale.value() / 100.0
        self.user_image_scale = max(0.1, min(5.0, scale))
        if self.current_image_path:
            try:
                img = Image.open(self.current_image_path).convert('RGB')
                self.canvas.set_user_image(img, self.offset_x, self.offset_y, self.user_image_scale)
            except Exception as e:
                self.log_error(f"更新缩放失败: {e}")

    def start_painting(self):
        if not self.current_image_path:
            self.show_message("错误", "请先上传图片")
            return

        mode_text = self.combo_mode.currentText()
        mode_enum = None
        for m in PaintMode:
            if m.value == mode_text:
                mode_enum = m
                break
        if mode_enum is None:
            self.show_message("错误", "无效模式")
            return

        mode_int = mode_enum.to_int()

        # ✅ 生成缩放后的临时图像
        try:
            original_img = Image.open(self.current_image_path).convert('RGB')
            scaled_w = int(original_img.width * self.user_image_scale)
            scaled_h = int(original_img.height * self.user_image_scale)
            if scaled_w < 1 or scaled_h < 1:
                raise ValueError("缩放后图像尺寸无效")
            scaled_img = original_img.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)

            # 创建临时文件
            with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as tmp:
                temp_path = tmp.name
            scaled_img.save(temp_path, 'PNG')
        except Exception as e:
            self.log_error(f"生成缩放图像失败: {e}")
            return

        cmd = [
            sys.executable, "Console.py",
            temp_path,
            str(self.offset_x),
            str(self.offset_y),
            str(mode_int)
        ]

        self.console_runner = ConsoleRunner(cmd, temp_file=temp_path)
        self.console_runner.output_received.connect(self.log_info)
        self.console_runner.finished.connect(self.on_console_finished)
        self.console_runner.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.label_status.setText("运行中...")
        self.log_info(f"启动命令: {' '.join(cmd)}")

    def stop_painting(self):
        if self.console_runner and self.console_runner.isRunning():
            self.console_runner.stop()
            self.console_runner.wait()
        self.on_console_finished(-1)

    def on_console_finished(self, exit_code: int):
        if self.console_runner:
            self.console_runner.cleanup_temp()
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        if exit_code == 0:
            self.label_status.setText("完成")
            self.log_info("绘画任务完成")
        elif exit_code == -1:
            self.label_status.setText("已停止")
            self.log_info("任务已停止")
        else:
            self.label_status.setText("失败")
            self.log_error(f"任务失败 (退出码: {exit_code})")
        self.console_runner = None

    def log_info(self, msg: str):
        match = PROGRESS_PATTERN.search(msg)
        if match:
            done = int(match.group(1))
            total = int(match.group(2))
            percent = float(match.group(3))
            fix_tasks = int(match.group(4))
            active = int(match.group(5))
            max_workers = int(match.group(6))

            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(done)
            self.label_status.setText(
                f"进度: {done}/{total} ({percent:.1f}%) | 修复: {fix_tasks} | 进程: {active}/{max_workers}"
            )
            self.text_log.append(f"[INFO] {msg}")
        else:
            self.text_log.append(f"[INFO] {msg}")

        sb = self.text_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def log_error(self, msg: str):
        self.text_log.append(f"[ERROR] {msg}")
        sb = self.text_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def show_message(self, title: str, content: str):
        QMessageBox.warning(self, title, content)

    def closeEvent(self, event):
        logger.info("Closing application...")

        # 停止线程（非阻塞）
        if self.console_runner and self.console_runner.isRunning():
            self.console_runner.stop()
            # 可选：启动一个定时器尝试清理，但不要阻塞主线程
            QTimer.singleShot(100, self.console_runner.cleanup_temp)

        if self.board_monitor.isRunning():
            self.board_monitor.stop()
            # 不要调用 .wait()！

        # 允许窗口立即关闭
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    setTheme(Theme.LIGHT)

    # 👇 添加这段：强制全局浅色背景
    palette = app.palette()
    palette.setColor(palette.ColorRole.Window, QColor(245, 245, 245))      # 背景色
    palette.setColor(palette.ColorRole.Base, QColor(255, 255, 255))        # 输入框背景
    palette.setColor(palette.ColorRole.WindowText, QColor(0, 0, 0))        # 文字颜色
    palette.setColor(palette.ColorRole.Text, QColor(0, 0, 0))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
