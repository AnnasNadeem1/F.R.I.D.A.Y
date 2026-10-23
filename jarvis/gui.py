"""The desktop app: an F.R.I.D.A.Y.-style HUD window plus a tray icon.

Run with jarvis.pyw (no console window). Closing the window hides it to the
tray; quit from the tray menu. Only one copy runs at a time: launching it
again just brings the existing window to the front.
"""
import ctypes
import html
import json
import logging
import math
import os
import random
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import QObject, QPointF, QProcess, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap,
                           QRadialGradient)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPushButton, QScrollArea, QSpinBox, QStackedWidget, QSystemTrayIcon, QTableWidget, QTableWidgetItem,
    QTextBrowser, QVBoxLayout, QWidget,
)

from .config import ROOT, settings, update_env
from .engine import Engine, Listener

log = logging.getLogger(__name__)

VOICES = [  # (label, TTS_ENGINE, KOKORO_VOICE)
    ("ElevenLabs - your custom voice from .env (online)", "elevenlabs", ""),
    ("Alan (Piper) - fastest, more robotic", "piper", ""),
    ("Bella (Kokoro) - natural, American female", "kokoro", "af_bella"),
    ("Bella + Nicole (Kokoro) - blend, smoother", "kokoro", "af_bella:1,af_nicole:1"),
    ("Heart (Kokoro) - natural, American female", "kokoro", "af_heart"),
    ("George (Kokoro) - natural, British", "kokoro", "bm_george"),
    ("Lewis (Kokoro) - natural, British", "kokoro", "bm_lewis"),
    ("Daniel (Kokoro) - natural, British", "kokoro", "bm_daniel"),
    ("Fable (Kokoro) - natural, British", "kokoro", "bm_fable"),
]
ELEVEN_MODELS = [  # (label, ELEVENLABS_MODEL)
    ("Flash v2.5 - fastest, half the credits", "eleven_flash_v2_5"),
    ("Turbo v2.5 - fast, half the credits", "eleven_turbo_v2_5"),
    ("Multilingual v2 - richest, ~1.5 s slower, full credits", "eleven_multilingual_v2"),
    ("v3 - most expressive, slowest, full credits", "eleven_v3"),
]
PHONE_STEPS = (
    "<ol style='margin-left:-20px; line-height:150%;'>"
    "<li>Install <b>Tailscale</b> on this PC and on your phone (Play Store), signed in to the <b>same account</b>: "
    "<a style='color:#ff9f43' href='https://tailscale.com/download'>tailscale.com/download</a></li>"
    "<li>In the <a style='color:#ff9f43' href='https://login.tailscale.com/admin/dns'>admin console DNS page</a>, "
    "make sure <b>MagicDNS</b> is on and click <b>Enable HTTPS</b>.</li>"
    "<li>Press <b>SET UP PHONE ACCESS</b> here.</li>"
    "<li>With Tailscale on, scan the QR code with your phone's camera and open the link in <b>Chrome</b>.</li>"
    "<li>Chrome menu: <b>Add to Home screen</b>. In the app, tap <b>ENABLE NOTIFICATIONS</b>.</li></ol>"
    "<p style='color:#5c626c;'>The QR code holds this PC's private pairing key, so only scan it on your own phone. "
    "UNPAIR ALL PHONES makes a new key.</p>"
)
INSTANCE_NAME = f"JarvisAssistant-{os.environ.get('USERNAME', 'user')}"
ICON_FILE = settings.data_dir / "jarvis.ico"

# ---------------------------------------------------------------- look

BG = "#07090d"
PANEL = "#0c1016"
TEXT = "#ece6dc"
DIM = "#7f8590"
AMBER = "#ff9f43"
TEAL = "#2fe3c4"
VIOLET = "#b48cff"
RED = "#ff4d5e"

STATE_COLOR = {
    "loading": "#8a8f98", "off": "#4b4f57", "sleeping": AMBER, "listening": TEAL,
    "thinking": VIOLET, "speaking": "#ffb86b", "error": RED,
}
STATE_LABEL = {
    "loading": "BOOTING", "off": "MIC OFFLINE", "sleeping": "STANDBY", "listening": "LISTENING",
    "thinking": "PROCESSING", "speaking": "SPEAKING", "error": "SYSTEM ERROR",
}
TITLE = "F.R.I.D.A.Y." if settings.name.lower() == "friday" else " ".join(settings.name.upper())
HEAD_FONT = "Bahnschrift"
MONO_FONT = "Cascadia Mono, Consolas"

STYLE = f"""
* {{ font-family: 'Segoe UI'; font-size: 13px; color: {TEXT}; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QWidget#page, QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}
QLabel#title {{ font-family: '{HEAD_FONT}'; font-size: 26px; font-weight: 600; letter-spacing: 6px; color: {AMBER}; }}
QLabel#state {{ font-family: '{HEAD_FONT}'; font-size: 13px; letter-spacing: 3px; }}
QLabel#activity {{ color: {DIM}; font-style: italic; }}
QLabel#status, QLabel#clock_date {{ font-family: {MONO_FONT}; font-size: 10px; color: #5c626c; }}
QLabel#clock {{ font-family: '{HEAD_FONT}'; font-size: 22px; color: {TEXT}; letter-spacing: 2px; }}
QLabel#hint {{ color: #5c626c; font-size: 11px; }}
QLabel#section {{ font-family: '{HEAD_FONT}'; font-size: 11px; letter-spacing: 3px; color: {AMBER}; }}
QLabel#live {{ font-family: {MONO_FONT}; color: {TEAL}; padding: 4px 2px; }}
QPushButton#nav {{ font-family: '{HEAD_FONT}'; font-size: 12px; letter-spacing: 3px; color: {DIM};
    background: transparent; border: none; border-bottom: 2px solid transparent; padding: 8px 14px; }}
QPushButton#nav:hover {{ color: {TEXT}; }}
QPushButton#nav:checked {{ color: {AMBER}; border-bottom: 2px solid {AMBER}; }}
QTextBrowser, QListWidget, QTableWidget {{ background: transparent; border: none;
    selection-background-color: rgba(255,159,67,0.25); selection-color: {TEXT}; }}
QListWidget::item {{ padding: 8px 4px; border-bottom: 1px solid #161b23; }}
QHeaderView::section {{ background: transparent; color: {AMBER}; border: none; border-bottom: 1px solid #2a2118;
    padding: 6px; font-family: '{HEAD_FONT}'; letter-spacing: 2px; font-size: 11px; }}
QTableWidget {{ gridline-color: #161b23; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: #0f141c; border: 1px solid #222a35;
    border-radius: 4px; padding: 7px 9px; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {AMBER}; }}
QLineEdit#prompt {{ font-family: {MONO_FONT}; font-size: 13px; padding: 10px 12px; }}
QComboBox QAbstractItemView {{ background: #0f141c; border: 1px solid #222a35; selection-background-color: #2a2118; }}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: none; }}
QPushButton {{ font-family: '{HEAD_FONT}'; letter-spacing: 1px; background: #10151d; border: 1px solid #262e3a;
    border-radius: 4px; padding: 7px 14px; }}
QPushButton:hover {{ border-color: {AMBER}; color: {AMBER}; }}
QPushButton#primary {{ background: rgba(255,159,67,0.12); border: 1px solid {AMBER}; color: {AMBER}; }}
QPushButton#primary:hover {{ background: rgba(255,159,67,0.22); }}
QPushButton#mic {{ font-family: '{HEAD_FONT}'; letter-spacing: 2px; border-radius: 14px; padding: 6px 16px; }}
QPushButton#mic:checked {{ background: rgba(47,227,196,0.10); border: 1px solid {TEAL}; color: {TEAL}; }}
QPushButton#mic:!checked {{ background: transparent; border: 1px solid #3a3f48; color: {DIM}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid #3a3f48; border-radius: 3px; background: #0f141c; }}
QCheckBox::indicator:checked {{ background: {AMBER}; border-color: {AMBER}; }}
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2a2f38; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {AMBER}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical, QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{ height: 0; background: none; }}
QToolTip {{ background: #0f141c; color: {TEXT}; border: 1px solid {AMBER}; }}
QMenu {{ background: #0f141c; border: 1px solid #2a2118; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; }}
QMenu::item:selected {{ background: rgba(255,159,67,0.18); color: {AMBER}; }}
QMenu::separator {{ height: 1px; background: #222a35; margin: 4px 8px; }}
"""


# ---------------------------------------------------------------- HUD drawing

def draw_hud(p: QPainter, cx: float, cy: float, size: float, color: QColor, t: float,
             level: float = 0.0, active: bool = False, detail: bool = True):
    """The F.R.I.D.A.Y. core: tick ring, rotating segment arcs, voice-reactive bars, glowing core."""
    s = size
    p.setRenderHint(QPainter.Antialiasing)

    def ring_rect(r):
        return QRectF(cx - r, cy - r, 2 * r, 2 * r)

    # Soft backdrop glow.
    glow = QRadialGradient(QPointF(cx, cy), s * 0.5)
    c = QColor(color)
    c.setAlpha(70 if active else 38)
    glow.setColorAt(0.0, c)
    c.setAlpha(0)
    glow.setColorAt(1.0, c)
    p.setPen(Qt.NoPen)
    p.setBrush(glow)
    p.drawEllipse(QPointF(cx, cy), s * 0.5, s * 0.5)

    # Outer tick ring, slowly turning.
    if detail:
        p.save()
        p.translate(cx, cy)
        p.rotate(t * 6)
        tick = QColor(color)
        for i in range(72):
            long_tick = i % 6 == 0
            tick.setAlpha(170 if long_tick else 70)
            p.setPen(QPen(tick, max(1.0, s * 0.008)))
            r1 = s * 0.46
            r2 = r1 - (s * 0.045 if long_tick else s * 0.022)
            a = math.radians(i * 5)
            p.drawLine(QPointF(math.cos(a) * r1, math.sin(a) * r1), QPointF(math.cos(a) * r2, math.sin(a) * r2))
        p.restore()

    # Segment arcs: faster when active.
    speed = 70 if active else 14
    arc = QColor(color)
    arc.setAlpha(230)
    p.setPen(QPen(arc, max(1.5, s * 0.03), Qt.SolidLine, Qt.FlatCap))
    p.setBrush(Qt.NoBrush)
    r = s * 0.37
    start = -t * speed
    for k in range(3):
        p.drawArc(ring_rect(r), int((start + k * 120) * 16), int(78 * 16))
    if detail:
        thin = QColor(color)
        thin.setAlpha(110)
        p.setPen(QPen(thin, max(1.0, s * 0.01), Qt.SolidLine, Qt.FlatCap))
        for k in range(4):
            p.drawArc(ring_rect(s * 0.31), int((t * speed * 0.6 + k * 90 + 20) * 16), int(48 * 16))

    # Voice-reactive bars around the core.
    if detail and level > 0.015:
        bar = QColor(color)
        bar.setAlpha(210)
        p.setPen(QPen(bar, max(1.2, s * 0.012), Qt.SolidLine, Qt.RoundCap))
        n = 48
        for i in range(n):
            a = 2 * math.pi * i / n
            wobble = 0.55 + 0.45 * math.sin(i * 1.7 + t * 9) * math.sin(i * 0.6 - t * 5)
            length = s * 0.09 * min(1.0, level * 1.6) * max(0.15, wobble)
            r1 = s * 0.2
            p.drawLine(QPointF(cx + math.cos(a) * r1, cy + math.sin(a) * r1),
                       QPointF(cx + math.cos(a) * (r1 + length), cy + math.sin(a) * (r1 + length)))

    # Core.
    pulse = 1 + (0.07 * math.sin(t * 6) if active else 0.03 * math.sin(t * 1.5)) + level * 0.18
    rc = s * 0.13 * pulse
    core = QRadialGradient(QPointF(cx - rc * 0.25, cy - rc * 0.25), rc * 1.3)
    core.setColorAt(0.0, QColor("#ffffff"))
    core.setColorAt(0.35, color.lighter(125))
    core.setColorAt(1.0, color.darker(260))
    p.setPen(QPen(color.lighter(140), max(1.0, s * 0.01)))
    p.setBrush(core)
    p.drawEllipse(QPointF(cx, cy), rc, rc)


def _draw_small_icon(p: QPainter, size: int, color: QColor):
    """Bold version for the tray (16-32 px): thick arcs and a big core, no fine detail."""
    p.setRenderHint(QPainter.Antialiasing)
    c = size / 2
    w = max(2.0, size * 0.12)
    r = size / 2 - w / 2 - 0.5
    p.setPen(QPen(color, w, Qt.SolidLine, Qt.FlatCap))
    p.setBrush(Qt.NoBrush)
    rect = QRectF(c - r, c - r, 2 * r, 2 * r)
    for k in range(3):
        p.drawArc(rect, int((100 + k * 120) * 16), int(85 * 16))
    rc = size * 0.22
    core = QRadialGradient(QPointF(c - rc * 0.3, c - rc * 0.3), rc * 1.4)
    core.setColorAt(0.0, QColor("#ffffff"))
    core.setColorAt(0.45, color.lighter(115))
    core.setColorAt(1.0, color.darker(150))
    p.setPen(Qt.NoPen)
    p.setBrush(core)
    p.drawEllipse(QPointF(c, c), rc, rc)


def hud_icon(state: str, size: int = 64) -> QIcon:
    """Tray/app icon in the state's colour. Includes bold small sizes for the tray and a
    detailed large one for the window, taskbar and Start Menu."""
    color = QColor(STATE_COLOR.get(state, AMBER))
    icon = QIcon()
    for px in sorted({16, 20, 24, 32, 48, size}):
        pm = QPixmap(px, px)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        if px <= 48:
            _draw_small_icon(p, px, color)
        else:
            draw_hud(p, px / 2, px / 2, px * 1.05, color, t=1.2, active=False, detail=px >= 96)
        p.end()
        icon.addPixmap(pm)
    return icon


class HudCore(QWidget):
    """Animated status core. Spins faster while active; bars follow the voice level."""

    def __init__(self, size=132):
        super().__init__()
        self.setFixedSize(size, size)
        self.color = QColor(STATE_COLOR["loading"])
        self.active = True
        self.level = 0.0
        self._target = 0.0
        self.t0 = time.monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(16)

    # Only animate while visible: no point redrawing 60 times a second in the tray.
    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start(16)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()

    def set_state(self, state: str):
        self.color = QColor(STATE_COLOR.get(state, AMBER))
        self.active = state in ("listening", "thinking", "speaking", "loading")

    def set_level(self, value: float):
        self._target = value

    def _tick(self):
        # Rise fast, fall slowly, like a VU meter.
        k = 0.5 if self._target > self.level else 0.12
        self.level += (self._target - self.level) * k
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        draw_hud(p, self.width() / 2, self.height() / 2, self.width(), self.color,
                 time.monotonic() - self.t0, self.level, self.active)


class HudPanel(QFrame):
    """A panel with a faint frame and HUD corner brackets."""

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, 6, 6)
        p.fillPath(path, QColor(PANEL))
        frame = QColor(AMBER)
        frame.setAlpha(34)
        p.setPen(QPen(frame, 1))
        p.drawPath(path)
        bracket = QColor(AMBER)
        bracket.setAlpha(190)
        p.setPen(QPen(bracket, 2))
        length = 16
        x0, y0, x1, y1 = r.left() + 1, r.top() + 1, r.right() - 1, r.bottom() - 1
        for (x, y, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx * length, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * length))


class Backdrop(QWidget):
    """Window background: near-black with a faint HUD grid."""

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(BG))
        grid = QColor(AMBER)
        grid.setAlpha(9)
        p.setPen(QPen(grid, 1))
        step = 28
        for x in range(0, self.width(), step):
            p.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), step):
            p.drawLine(0, y, self.width(), y)


def _dark_title_bar(widget: QWidget):
    """Windows 11: dark title bar coloured to match the window, so it blends in."""
    try:
        hwnd = int(widget.winId())
        dwm = ctypes.windll.dwmapi

        def set_attr(attr, value):
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        def colorref(hex_color):  # "#rrggbb" -> 0x00bbggrr
            r, g, b = int(hex_color[1:3], 16), int(hex_color[3:5], 16), int(hex_color[5:7], 16)
            return (b << 16) | (g << 8) | r

        set_attr(20, 1)  # DWMWA_USE_IMMERSIVE_DARK_MODE
        set_attr(35, colorref(BG))  # DWMWA_CAPTION_COLOR
        set_attr(36, colorref(AMBER))  # DWMWA_TEXT_COLOR
        set_attr(34, colorref("#2a2118"))  # DWMWA_BORDER_COLOR
    except Exception:
        pass


# ---------------------------------------------------------------- engine -> Qt

class Signals(QObject):
    state = Signal(str)
    status = Signal(str)
    partial = Signal(str)
    user = Signal(str, str)
    a_start = Signal()
    a_delta = Signal(str)
    a_end = Signal()
    notice = Signal(str, str)
    error = Signal(str)
    level = Signal(float)
    activity = Signal(str)


class QtListener(Listener):
    """Forwards engine events (from background threads) to the GUI thread as signals."""

    def __init__(self, s: Signals):
        self.s = s
        self._last_level = 0.0

    def state(self, state): self.s.state.emit(state)
    def status(self, text): self.s.status.emit(text)
    def user_partial(self, text): self.s.partial.emit(text)
    def user(self, text, source): self.s.user.emit(text, source)
    def assistant_start(self): self.s.a_start.emit()
    def assistant_delta(self, chunk): self.s.a_delta.emit(chunk)
    def assistant_end(self): self.s.a_end.emit()
    def notice(self, kind, text): self.s.notice.emit(kind, text)
    def error(self, text): self.s.error.emit(text)
    def activity(self, text): self.s.activity.emit(text)

    def level(self, value):
        # Levels arrive every 30-80 ms; skip tiny changes to keep the event queue light.
        if abs(value - self._last_level) > 0.02 or value == 0.0:
            self._last_level = value
            self.s.level.emit(value)


def _table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels([h.upper() for h in headers])
    t.verticalHeader().setVisible(False)
    t.setShowGrid(False)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return t


def _label(text: str) -> QLabel:
    """Settings labels share one width, so fields line up across sections."""
    lbl = QLabel(text, wordWrap=True)
    lbl.setFixedWidth(300)
    return lbl


def _page(margins=(16, 14, 16, 14)) -> tuple[QWidget, QVBoxLayout]:
    w = QWidget(objectName="page")
    v = QVBoxLayout(w)
    v.setContentsMargins(*margins)
    v.setSpacing(10)
    return w, v


# ---------------------------------------------------------------- window

class MainWindow(QMainWindow):
    def __init__(self, signals: Signals, engine: Engine):
        super().__init__()
        self.engine = engine
        self.messages: list[dict] = []
        self._render_pending = False
        self._hint_shown = False
        self._titled = False
        self.quitting = False
        self.setWindowTitle(settings.name)
        self.setWindowIcon(hud_icon("sleeping", 256))
        self.resize(860, 720)
        self.setMinimumSize(600, 560)

        root = Backdrop(objectName="root")
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(20, 14, 20, 18)
        lay.setSpacing(12)
        lay.addLayout(self._header())

        # Navigation bar + pages inside a HUD panel.
        panel = HudPanel()
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(10, 6, 10, 10)
        pv.setSpacing(0)
        nav = QHBoxLayout()
        nav.setSpacing(4)
        self.stack = QStackedWidget()
        self.nav_group = QButtonGroup(self)
        self.pages = {}
        for i, (name, build) in enumerate((("Chat", self._chat_page), ("Reminders", self._reminders_page),
                                           ("Memory", self._memory_page), ("Claude", self._claude_page),
                                           ("Phone", self._phone_page), ("Settings", self._settings_page))):
            b = QPushButton(name.upper(), objectName="nav", checkable=True)
            self.nav_group.addButton(b, i)
            nav.addWidget(b)
            self.stack.addWidget(build())
            self.pages[name] = i
        nav.addStretch(1)
        self.nav_group.idClicked.connect(self._go)
        self.nav_group.button(0).setChecked(True)
        pv.addLayout(nav)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background: #1d232c;")
        pv.addWidget(line)
        pv.addWidget(self.stack, 1)
        lay.addWidget(panel, 1)

        refresher = QTimer(self)
        refresher.timeout.connect(self.refresh_current_page)
        refresher.start(4000)
        clock = QTimer(self)
        clock.timeout.connect(self._tick_clock)
        clock.start(1000)
        self._tick_clock()

        signals.state.connect(self.on_state)
        signals.status.connect(self.status_label.setText)
        signals.partial.connect(self.on_partial)
        signals.user.connect(lambda text, src: self.add_message("user", text, src))
        signals.a_start.connect(lambda: self.add_message("assistant", ""))
        signals.a_delta.connect(self.on_delta)
        signals.a_end.connect(self._end_reply)
        signals.notice.connect(lambda kind, text: self.add_message("notice", text, kind))
        signals.error.connect(lambda text: self.add_message("error", text))
        signals.level.connect(self.core.set_level)
        signals.activity.connect(self.on_activity)
        self.on_state("loading")
        self._render()

    # ---- header

    def _header(self) -> QHBoxLayout:
        head = QHBoxLayout()
        head.setSpacing(16)
        self.core = HudCore()
        head.addWidget(self.core)
        names = QVBoxLayout()
        names.setSpacing(2)
        names.addStretch(1)
        names.addWidget(QLabel(TITLE, objectName="title"))
        self.state_label = QLabel("", objectName="state")
        names.addWidget(self.state_label)
        self.activity_label = QLabel("", objectName="activity")
        names.addWidget(self.activity_label)
        self.status_label = QLabel("Starting up...", objectName="status", wordWrap=True)
        names.addWidget(self.status_label)
        names.addStretch(1)
        head.addLayout(names, 1)
        side = QVBoxLayout()
        side.setSpacing(2)
        self.clock = QLabel("", objectName="clock", alignment=Qt.AlignRight)
        self.clock_date = QLabel("", objectName="clock_date", alignment=Qt.AlignRight)
        side.addWidget(self.clock)
        side.addWidget(self.clock_date)
        side.addSpacing(8)
        self.toggle = QPushButton(objectName="mic", checkable=True)
        self.toggle.setChecked(self.engine.listening)
        self.toggle.toggled.connect(self.set_listening)
        self.toggle.setToolTip("Turning the mic off releases it completely. Reminders and Claude alerts still play.")
        self._sync_toggle_text()
        side.addWidget(self.toggle, 0, Qt.AlignRight)
        side.addStretch(1)
        head.addLayout(side)
        return head

    def _tick_clock(self):
        now = datetime.now()
        self.clock.setText(now.strftime("%H:%M"))
        self.clock_date.setText(now.strftime("%a %d %b %Y").upper())

    # ---- pages

    def _go(self, index: int):
        self.stack.setCurrentIndex(index)
        self.refresh_current_page()

    def _chat_page(self) -> QWidget:
        w, v = _page((14, 10, 14, 12))
        self.chat = QTextBrowser(openExternalLinks=True)
        v.addWidget(self.chat, 1)
        self.live = QLabel("", objectName="live", wordWrap=True)
        self.live.hide()
        v.addWidget(self.live)
        row = QHBoxLayout()
        self.input = QLineEdit(objectName="prompt", placeholderText=f"›  Ask {settings.name}...")
        self.input.returnPressed.connect(self.send_typed)
        row.addWidget(self.input, 1)
        send = QPushButton("SEND", objectName="primary")
        send.clicked.connect(self.send_typed)
        row.addWidget(send)
        v.addLayout(row)
        return w

    def _reminders_page(self) -> QWidget:
        w, v = _page()
        v.addWidget(QLabel("UPCOMING REMINDERS", objectName="section"))
        self.rem_table = _table(["When", "Reminder"])
        self.rem_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        v.addWidget(self.rem_table, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel('Ask out loud to add one, e.g. "remind me in 20 minutes to stretch".',
                             objectName="hint"), 1)
        cancel = QPushButton("CANCEL SELECTED")
        cancel.clicked.connect(self.cancel_reminder)
        row.addWidget(cancel)
        v.addLayout(row)
        return w

    def _memory_page(self) -> QWidget:
        w, v = _page()
        v.addWidget(QLabel("LONG-TERM MEMORY", objectName="section"))
        v.addWidget(QLabel(f"What {settings.name} knows about you. She reads all of this in every conversation.",
                           objectName="hint"))
        self.mem_list = QListWidget()
        self.mem_list.setWordWrap(True)
        v.addWidget(self.mem_list, 1)
        row = QHBoxLayout()
        self.mem_input = QLineEdit(placeholderText="Add something to remember, e.g. My sister's name is Ayesha.")
        self.mem_input.returnPressed.connect(self.add_memory)
        row.addWidget(self.mem_input, 1)
        add = QPushButton("ADD")
        add.clicked.connect(self.add_memory)
        row.addWidget(add)
        forget = QPushButton("FORGET SELECTED")
        forget.clicked.connect(self.forget_memory)
        row.addWidget(forget)
        v.addLayout(row)
        return w

    def _claude_page(self) -> QWidget:
        w, v = _page()
        v.addWidget(QLabel("CLAUDE CODE SESSIONS", objectName="section"))
        self.claude_table = _table(["Project", "State", "Since", "Last message"])
        for i in range(3):
            self.claude_table.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.claude_table.setWordWrap(True)
        v.addWidget(self.claude_table, 1)
        v.addWidget(QLabel(f"Sessions report here while {settings.name} is running "
                           "(set up once with: python -m jarvis --install-claude-hooks).", objectName="hint"))
        return w

    def _phone_page(self) -> QWidget:
        w, v = _page()
        v.addWidget(QLabel("PHONE ACCESS", objectName="section"))
        v.addWidget(QLabel(f"Talk to {settings.name} from your Android phone, anywhere, through Tailscale: a private "
                           "encrypted link between your own devices. Nothing is exposed to the internet.",
                           objectName="hint", wordWrap=True))
        self.phone_status = QLabel("Checking Tailscale...", wordWrap=True)
        v.addWidget(self.phone_status)
        row = QHBoxLayout()
        self.btn_serve = QPushButton("SET UP PHONE ACCESS", objectName="primary")
        self.btn_serve.clicked.connect(self.setup_phone)
        self.btn_serve.hide()
        row.addWidget(self.btn_serve)
        test = QPushButton("TEST NOTIFICATION")
        test.clicked.connect(self.test_push)
        row.addWidget(test)
        unpair = QPushButton("UNPAIR ALL PHONES")
        unpair.clicked.connect(self.unpair_phones)
        row.addWidget(unpair)
        row.addStretch(1)
        v.addLayout(row)
        body = QHBoxLayout()
        body.setSpacing(18)
        self.qr = QLabel(alignment=Qt.AlignCenter)
        self.qr.setFixedSize(236, 236)
        self.qr.setStyleSheet("background: #ffffff; border-radius: 6px; color: #333;")
        body.addWidget(self.qr, 0, Qt.AlignTop)
        self.phone_steps = QLabel(PHONE_STEPS, wordWrap=True, textFormat=Qt.RichText)
        self.phone_steps.setOpenExternalLinks(True)
        body.addWidget(self.phone_steps, 1, Qt.AlignTop)
        v.addLayout(body)
        v.addStretch(1)
        self._phone_info = None
        self.signals_phone = Signals()
        self.signals_phone.status.connect(self._show_phone)
        return w

    def _refresh_phone(self):
        """Ask Tailscale in the background (it's a subprocess); update the page when it answers."""
        def run():
            from .remote import push, tailscale
            st = tailscale.status()
            st["serving"] = bool(st["running"] and tailscale.serving(settings.phone_port))
            st["phones"] = push.count()
            self.signals_phone.status.emit(json.dumps(st))
        threading.Thread(target=run, daemon=True).start()

    def _show_phone(self, payload: str):
        from .remote.server import token

        st = json.loads(payload)
        if st == self._phone_info:
            return
        self._phone_info = st
        ready = bool(st["running"] and st["dns_name"] and st["serving"])
        if not st["installed"]:
            msg, color = "Tailscale isn't installed on this PC yet (step 1).", AMBER
        elif not st["running"] or not st["dns_name"]:
            msg, color = st["problem"], AMBER
        elif not st["serving"]:
            msg, color = f"Tailscale is connected as {st['dns_name']}. Press SET UP PHONE ACCESS.", AMBER
        else:
            msg, color = (f"READY  ·  https://{st['dns_name']}  ·  "
                          f"{st['phones']} phone(s) with notifications on"), TEAL
        self.phone_status.setText(msg)
        self.phone_status.setStyleSheet(f"color: {color};")
        self.btn_serve.setVisible(bool(st["running"] and st["dns_name"] and not st["serving"]))
        if ready:
            self._draw_qr(f"https://{st['dns_name']}/#pair={token()}")
        else:
            self.qr.clear()
            self.qr.setText("QR code appears\nonce setup is done")

    def _draw_qr(self, url: str):
        import io as _io

        import qrcode

        img = qrcode.make(url, box_size=6, border=2)
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        pm = QPixmap()
        pm.loadFromData(buf.getvalue(), "PNG")
        self.qr.setPixmap(pm.scaled(224, 224, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def setup_phone(self):
        from .remote import tailscale

        ok, out = tailscale.serve(settings.phone_port)
        if ok:
            QMessageBox.information(self, settings.name, "Phone access is set up. Scan the QR code with your phone.")
        else:
            QMessageBox.warning(self, settings.name, f"Tailscale couldn't set it up:\n\n{out}")
        self._phone_info = None
        self._refresh_phone()

    def test_push(self):
        from .remote import push

        if not push.count():
            QMessageBox.information(self, settings.name, "No phone has turned on notifications yet: open the app on "
                                    "your phone and tap ENABLE NOTIFICATIONS.")
            return
        push.notify(settings.name, "Test notification from your PC.", tag="test")

    def unpair_phones(self):
        from .remote.server import reset_token

        answer = QMessageBox.question(self, settings.name, "Unpair every phone? They'll need to scan the new QR code.")
        if answer == QMessageBox.Yes:
            reset_token()
            self._phone_info = None
            self._refresh_phone()

    def _settings_page(self) -> QWidget:
        from . import winstartup

        w, outer = _page((4, 4, 4, 4))
        scroll = QScrollArea(widgetResizable=True, frameShape=QFrame.NoFrame)
        inner, v = _page((12, 10, 12, 12))
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        def section(title):
            v.addSpacing(6)
            v.addWidget(QLabel(title, objectName="section"))
            f = QFormLayout()
            f.setVerticalSpacing(9)
            f.setLabelAlignment(Qt.AlignLeft)
            v.addLayout(f)
            return f

        f = section("GENERAL")
        self.chk_startup = QCheckBox(f"Start {settings.name} when I sign in to Windows")
        self.chk_startup.setChecked(winstartup.is_startup_enabled())
        self.chk_startup.toggled.connect(self.set_startup)
        f.addRow(self.chk_startup)
        self.chk_speak_typed = QCheckBox("Speak replies to typed messages")
        self.chk_speak_typed.setChecked(settings.speak_typed)
        self.chk_speak_typed.toggled.connect(lambda on: setattr(self.engine, "speak_typed", on))
        f.addRow(self.chk_speak_typed)

        f = section("LISTENING")
        self.sp_wake = QDoubleSpinBox(minimum=0.1, maximum=0.95, singleStep=0.05, value=settings.wake_threshold)
        f.addRow(_label("Wake word threshold (lower = wakes more easily)"), self.sp_wake)
        self.sp_follow = QSpinBox(minimum=2, maximum=30, value=int(settings.followup_seconds), suffix=" s")
        f.addRow(_label("Follow-up listening window"), self.sp_follow)
        self.chk_barge = QCheckBox("Say my name while I'm talking to interrupt me (best with headphones)")
        self.chk_barge.setChecked(settings.barge_in)
        f.addRow(self.chk_barge)
        self.chk_pause = QCheckBox(f'Ignore my name mid-sentence ("see you on {settings.name}")')
        self.chk_pause.setChecked(settings.wake_require_pause)
        f.addRow(self.chk_pause)
        self.cb_stt = QComboBox()
        self.cb_stt.addItem("Groq Whisper (online, best with accents)", "groq")
        self.cb_stt.addItem("Local Whisper (private, less accurate)", "local")
        self.cb_stt.setCurrentIndex(0 if settings.stt_engine == "groq" else 1)
        f.addRow(_label("Speech recognition"), self.cb_stt)

        f = section("VOICE")
        self.cb_voice = QComboBox()
        for label, engine, voice in VOICES:
            self.cb_voice.addItem(label, (engine, voice))
        current = next((i for i, (_, e, vv) in enumerate(VOICES)
                        if e == settings.tts_engine and (e != "kokoro" or vv == settings.kokoro_voice)), 0)
        self.cb_voice.setCurrentIndex(current)
        preview = QPushButton("PREVIEW")
        preview.clicked.connect(self.preview_voice)
        voice_row = QHBoxLayout()
        voice_row.addWidget(self.cb_voice, 1)
        voice_row.addWidget(preview)
        f.addRow(_label("Voice"), voice_row)
        self.cb_11model = QComboBox()
        for label, model in ELEVEN_MODELS:
            self.cb_11model.addItem(label, model)
        self.cb_11model.setCurrentIndex(next((i for i, (_, m) in enumerate(ELEVEN_MODELS)
                                              if m == settings.elevenlabs_model), 0))
        f.addRow(_label("ElevenLabs model"), self.cb_11model)
        self.sp_speed = QDoubleSpinBox(minimum=0.7, maximum=1.6, singleStep=0.05, value=settings.tts_speed)
        f.addRow(_label("Local voice pace (higher = slower)"), self.sp_speed)
        self.say_input = QLineEdit(placeholderText="Type a word or name to hear it, e.g. Annas")
        self.say_input.returnPressed.connect(self.say_test)
        say_btn = QPushButton("SAY IT")
        say_btn.clicked.connect(self.say_test)
        edit_btn = QPushButton("EDIT PRONUNCIATIONS")
        edit_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ROOT / "pronunciations.txt"))))
        say_row = QHBoxLayout()
        say_row.addWidget(self.say_input, 1)
        say_row.addWidget(say_btn)
        say_row.addWidget(edit_btn)
        f.addRow(_label("Pronunciation"), say_row)

        f = section("GOOGLE CLASSROOM")
        self.classroom_state = QLabel("", objectName="hint")
        connect = QPushButton("CONNECT GOOGLE CLASSROOM")
        connect.clicked.connect(self.connect_classroom)
        cr_row = QHBoxLayout()
        cr_row.addWidget(connect)
        cr_row.addWidget(self.classroom_state, 1)
        f.addRow(cr_row)
        self.chk_classroom = QCheckBox("Remind me before assignments are due (until I turn them in)")
        self.chk_classroom.setChecked(settings.classroom_reminders)
        f.addRow(self.chk_classroom)
        self.ed_classroom_hours = QLineEdit(settings.classroom_remind_hours)
        self.ed_classroom_hours.setMaximumWidth(140)
        self.ed_classroom_hours.setToolTip("Hours before the due time, comma-separated, e.g. 24,2")
        f.addRow(_label("Remind this many hours before (e.g. 24,2)"), self.ed_classroom_hours)
        self._update_classroom_state()

        f = section("DAILY BRIEFING")
        self.ed_brief_time = QLineEdit(settings.briefing_time)
        self.ed_brief_time.setPlaceholderText("07:30  (empty = only when I ask)")
        self.ed_brief_time.setMaximumWidth(220)
        f.addRow(_label("Give it automatically at (24-hour HH:MM)"), self.ed_brief_time)
        self.ed_brief_place = QLineEdit(settings.briefing_location)
        self.ed_brief_place.setMaximumWidth(220)
        f.addRow(_label("Weather for"), self.ed_brief_place)
        brief_now = QPushButton("BRIEF ME NOW")
        brief_now.clicked.connect(lambda: threading.Thread(target=self.engine.run_briefing, daemon=True).start())
        f.addRow(brief_now)

        f = section("CLAUDE TASKS BY VOICE")
        self.chk_ctasks = QCheckBox('Let me start Claude Code tasks by voice ("ask Claude to ...")')
        self.chk_ctasks.setChecked(settings.claude_tasks)
        f.addRow(self.chk_ctasks)
        self.cb_cmodel = QComboBox()
        for label, m in (("Sonnet (balanced)", "sonnet"), ("Haiku (cheapest on your usage)", "haiku"),
                         ("Opus (strongest, uses the most)", "opus")):
            self.cb_cmodel.addItem(label, m)
        self.cb_cmodel.setCurrentIndex(max(0, self.cb_cmodel.findData(settings.claude_task_model)))
        f.addRow(_label("Model for these tasks"), self.cb_cmodel)
        self.sp_cminutes = QSpinBox(minimum=1, maximum=60, value=settings.claude_task_minutes, suffix=" min")
        self.sp_cminutes.setMaximumWidth(140)
        f.addRow(_label("Stop a task after"), self.sp_cminutes)
        f.addRow(QLabel("Allowed projects: " + (", ".join(__import__("jarvis.claude_tasks", fromlist=["projects"]).projects())
                                               or "none") + "  (edit CLAUDE_PROJECTS in .env)", objectName="hint"))

        f = section("CLAUDE CODE")
        self.sp_claude = QSpinBox(minimum=0, maximum=600, value=int(settings.claude_announce_min_seconds), suffix=" s")
        f.addRow(_label("Announce tasks that took longer than"), self.sp_claude)
        for sp in (self.sp_wake, self.sp_follow, self.sp_speed, self.sp_claude):
            sp.setMaximumWidth(140)
            sp.setToolTip("Type a value, or scroll / use the arrow keys")

        v.addSpacing(8)
        v.addWidget(QLabel("Changes apply after the restart. Numbers: type, or use the mouse wheel / arrow keys.",
                           objectName="hint"))
        row = QHBoxLayout()
        save = QPushButton("SAVE AND RESTART", objectName="primary")
        save.clicked.connect(self.save_settings)
        row.addWidget(save)
        row.addStretch(1)
        v.addLayout(row)

        v.addSpacing(10)
        v.addWidget(QLabel("TOOLS", objectName="section"))
        row2 = QHBoxLayout()
        for label, slot in (("DIAGNOSTICS", self.run_diagnostics), ("ADD TO START MENU", self.add_shortcut),
                            ("DATA FOLDER", self.open_data), ("LOG", self.open_log)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            row2.addWidget(b)
        row2.addStretch(1)
        v.addLayout(row2)
        v.addStretch(1)
        return w

    # ---- chat

    def add_message(self, role: str, text: str, extra: str = ""):
        self.messages.append({"role": role, "text": text, "extra": extra, "time": datetime.now().strftime("%H:%M")})
        self.messages = self.messages[-200:]
        self._schedule_render()

    def on_delta(self, chunk: str):
        if self.messages and self.messages[-1]["role"] == "assistant":
            self.messages[-1]["text"] += chunk
            self._schedule_render()

    def _end_reply(self):
        # Drop an empty reply bubble (e.g. interrupted before a word was said).
        if self.messages and self.messages[-1]["role"] == "assistant" and not self.messages[-1]["text"].strip():
            self.messages.pop()
        self._schedule_render()

    def on_partial(self, text: str):
        self.live.setText(f"◉  {text}" if text else "")
        self.live.setVisible(bool(text))

    def on_activity(self, text: str):
        self.activity_label.setText(f"{text}..." if text else "")

    def _schedule_render(self):
        if not self._render_pending:
            self._render_pending = True
            QTimer.singleShot(40, self._render)

    def _render(self):
        self._render_pending = False
        mono = "font-family:'Cascadia Mono','Consolas'; font-size:10px; letter-spacing:2px;"
        out = []
        for m in self.messages:
            t = html.escape(m["text"]).replace("\n", "<br>")
            if m["role"] == "user":
                tag = {"typed": " · TYPED", "phone": " · PHONE"}.get(m["extra"], "")
                out.append(f'<p style="margin:16px 0 3px 0; {mono} color:{TEAL};">▸ YOU · {m["time"]}{tag}</p>'
                           f'<p style="margin:0; color:{TEXT};">{t}</p>')
            elif m["role"] == "assistant":
                out.append(f'<p style="margin:16px 0 3px 0; {mono} color:{AMBER};">◂ {settings.name.upper()} · '
                           f'{m["time"]}</p><p style="margin:0; color:#f4dcc0;">{t or "…"}</p>')
            elif m["role"] == "notice" and m["extra"] == "interrupted":
                out.append(f'<p style="margin:6px 0 0 0; {mono} color:#5c626c;">— INTERRUPTED —</p>')
            elif m["role"] == "notice":
                icon = {"reminder": "⏰", "classroom": "📚", "briefing": "☀"}.get(m["extra"], "⚡")
                out.append(f'<p style="margin:16px 0 0 0; color:{AMBER};">{icon} {t}</p>')
            else:
                out.append(f'<p style="margin:16px 0 0 0; color:{RED};">⚠ {t}</p>')
        if not out:
            phrase = getattr(self.engine, "wake_phrase", settings.name)
            out.append(f'<p style="margin-top:30px; {mono} color:#5c626c;">STANDING BY. SAY "{phrase.upper()}" '
                       f'OR TYPE BELOW.</p>')
        self.chat.setHtml("".join(out))
        bar = self.chat.verticalScrollBar()
        bar.setValue(bar.maximum())

    def send_typed(self):
        text = self.input.text().strip()
        if text:
            self.input.clear()
            self.engine.submit_text(text)

    # ---- state

    def on_state(self, state: str):
        self.core.set_state(state)
        color = STATE_COLOR.get(state, AMBER)
        label = STATE_LABEL.get(state, state.upper())
        if state == "sleeping":
            phrase = getattr(self.engine, "wake_phrase", settings.name)
            label += f'  ·  SAY "{phrase.upper()}"'
        self.state_label.setText(f"●  {label}")
        self.state_label.setStyleSheet(f"color: {color};")
        if state != "listening":
            self.on_partial("")
        if state in ("sleeping", "listening", "off"):
            self.on_activity("")
        app = QApplication.instance()
        if hasattr(app, "tray"):
            app.tray.setIcon(hud_icon(state))
            app.tray.setToolTip(f"{settings.name}: {STATE_LABEL.get(state, state).title()}")

    def set_listening(self, on: bool):
        self.engine.set_listening(on)
        for control in (self.toggle, getattr(QApplication.instance(), "act_listen", None)):
            if control is not None and control.isChecked() != on:
                control.blockSignals(True)
                control.setChecked(on)
                control.blockSignals(False)
        self._sync_toggle_text()

    def _sync_toggle_text(self):
        self.toggle.setText("●  MIC LIVE" if self.toggle.isChecked() else "○  MIC OFF")

    # ---- data pages

    def refresh_current_page(self):
        if not self.isVisible():
            return
        name = next(n for n, i in self.pages.items() if i == self.stack.currentIndex())
        try:
            if name == "Reminders":
                self._fill_reminders()
            elif name == "Memory":
                self._fill_memory()
            elif name == "Claude":
                self._fill_claude()
            elif name == "Phone":
                self._refresh_phone()
            elif name == "Settings":
                self._update_classroom_state()
        except Exception:
            log.exception("refresh failed")

    def _fill_reminders(self):
        from .reminders import list_reminders

        items = list_reminders()["reminders"]
        self.rem_table.setRowCount(len(items))
        for row, r in enumerate(items):
            when = QTableWidgetItem(r["due"])
            when.setData(Qt.UserRole, r["id"])
            self.rem_table.setItem(row, 0, when)
            self.rem_table.setItem(row, 1, QTableWidgetItem(r["text"]))

    def cancel_reminder(self):
        from .reminders import cancel_reminder

        row = self.rem_table.currentRow()
        if row >= 0:
            cancel_reminder(self.rem_table.item(row, 0).data(Qt.UserRole))
            self._fill_reminders()

    def _fill_memory(self):
        from .store import db

        with db() as c:
            rows = c.execute("SELECT id, text FROM facts ORDER BY id").fetchall()
        selected = self.mem_list.currentItem().data(Qt.UserRole) if self.mem_list.currentItem() else None
        self.mem_list.clear()
        for r in rows:
            item = QListWidgetItem(r["text"])
            item.setData(Qt.UserRole, r["id"])
            self.mem_list.addItem(item)
            if r["id"] == selected:
                self.mem_list.setCurrentItem(item)

    def add_memory(self):
        from .memory import remember

        text = self.mem_input.text().strip()
        if text:
            remember(text)
            self.mem_input.clear()
            self._fill_memory()

    def forget_memory(self):
        from .memory import forget

        item = self.mem_list.currentItem()
        if item:
            forget(item.data(Qt.UserRole))
            self._fill_memory()

    def _fill_claude(self):
        from .claude_bridge import claude_code_status

        sessions = claude_code_status()["sessions"]
        self.claude_table.setRowCount(len(sessions))
        for row, s in enumerate(sessions):
            detail = s["waiting_for"] or s["last_message"]
            for col, val in enumerate((s["project"], s["state"].upper(), s["since"], detail)):
                item = QTableWidgetItem(val)
                if col == 1:
                    item.setForeground(QColor(RED if s["state"] == "needs you" else TEAL if s["state"] == "working"
                                              else AMBER))
                self.claude_table.setItem(row, col, item)
        self.claude_table.resizeRowsToContents()

    # ---- settings

    def set_startup(self, on: bool):
        from . import winstartup

        try:
            winstartup.set_startup(on)
        except OSError as e:
            QMessageBox.warning(self, settings.name, f"Couldn't change the startup setting: {e}")
        for control in (getattr(QApplication.instance(), "act_startup", None), self.chk_startup):
            if control is not None and control.isChecked() != on:
                control.blockSignals(True)
                control.setChecked(on)
                control.blockSignals(False)

    def say_test(self):
        """Speak with the current voice, after pronunciations.txt is applied (it's re-read on change)."""
        text = self.say_input.text().strip()
        speaker = getattr(self.engine, "speaker", None)
        if text and speaker:
            speaker.say(text)

    def preview_voice(self):
        """Hear the selected voice without switching to it (loads it separately, ~3 s)."""
        engine, voice = self.cb_voice.currentData()
        model = self.cb_11model.currentData()
        pace = self.sp_speed.value()
        line = random.choice(("All systems are green. What are we working on?",
                              "Good to see you. Everything's running smoothly.",
                              "Standing by. Just say the word."))

        def run():
            try:
                import sounddevice as sd

                from .speech.tts import ElevenLabsEngine, KokoroEngine, PiperEngine, Pronunciations
                if engine == "elevenlabs":
                    e = ElevenLabsEngine(settings.elevenlabs_api_key, settings.elevenlabs_voice_id, model,
                                         PiperEngine(settings.piper_voice, settings.models_dir, pace))
                elif engine == "kokoro":
                    e = KokoroEngine(voice, settings.models_dir, pace)
                else:
                    e = PiperEngine(settings.piper_voice, settings.models_dir, pace)
                text = Pronunciations(ROOT / "pronunciations.txt").apply(line)
                sd.play(e.synth(text), e.sample_rate)
                sd.wait()
                if getattr(e, "last_error", ""):
                    self.engine.l.error(f"ElevenLabs didn't work, so that was the backup voice: {e.last_error}")
            except Exception as ex:
                self.engine.l.error(f"Voice preview failed: {ex}")

        threading.Thread(target=run, daemon=True).start()

    def save_settings(self):
        engine, voice = self.cb_voice.currentData()
        update_env({
            "TTS_ENGINE": engine,
            "ELEVENLABS_MODEL": self.cb_11model.currentData(),
            "KOKORO_VOICE": voice or settings.kokoro_voice,
            "WAKE_THRESHOLD": f"{self.sp_wake.value():.2f}",
            "FOLLOWUP_SECONDS": str(self.sp_follow.value()),
            "TTS_SPEED": f"{self.sp_speed.value():.2f}",
            "STT_ENGINE": self.cb_stt.currentData(),
            "CLAUDE_ANNOUNCE_MIN_SECONDS": str(self.sp_claude.value()),
            "SPEAK_TYPED": "1" if self.chk_speak_typed.isChecked() else "0",
            "BARGE_IN": "1" if self.chk_barge.isChecked() else "0",
            "WAKE_REQUIRE_PAUSE": "1" if self.chk_pause.isChecked() else "0",
            "CLAUDE_TASKS": "1" if self.chk_ctasks.isChecked() else "0",
            "CLAUDE_TASK_MODEL": self.cb_cmodel.currentData(),
            "CLAUDE_TASK_MINUTES": str(self.sp_cminutes.value()),
            "BRIEFING_TIME": self.ed_brief_time.text().strip(),
            "BRIEFING_LOCATION": self.ed_brief_place.text().strip() or "Islamabad",
            "CLASSROOM_REMINDERS": "1" if self.chk_classroom.isChecked() else "0",
            "CLASSROOM_REMIND_HOURS": ",".join(p.strip() for p in self.ed_classroom_hours.text().split(",")
                                               if p.strip().replace(".", "", 1).isdigit()) or "24,2",
        })
        QApplication.instance().restart()

    def _update_classroom_state(self):
        from .classroom import connected

        self.classroom_state.setText("Connected." if connected() else "Not connected yet.")

    def connect_classroom(self):
        """Google sign-in in the browser (pick the account your Classroom is on), in the background."""
        QMessageBox.information(self, settings.name,
                                "Your browser will open to sign in to Google.\n\nPick the account your Classroom is on "
                                "(often your school account). Friday only asks to view your classes and your own "
                                "assignments; she can't change or submit anything.")

        def run():
            from . import classroom
            try:
                classroom.login()
                r = classroom.classroom_courses()
                if "error" in r:
                    self.engine.l.error(r["error"])
                else:
                    classroom.sync_reminders()
                    self.engine.l.notice("classroom", f"Google Classroom connected: {len(r['courses'])} classes.")
            except Exception as e:
                log.exception("classroom login failed")
                self.engine.l.error(f"Couldn't connect Google Classroom: {e}")
        threading.Thread(target=run, daemon=True).start()

    def run_diagnostics(self):
        DiagnosticsDialog(self).open()

    def add_shortcut(self):
        from . import winstartup

        try:
            winstartup.create_start_menu_shortcut(ICON_FILE)
            QMessageBox.information(self, settings.name,
                                    f"Added {settings.name} to the Start Menu. You can pin it from there.")
        except Exception as e:
            QMessageBox.warning(self, settings.name, f"Couldn't create the shortcut: {e}")

    def open_data(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(settings.data_dir)))

    def open_log(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(settings.data_dir / "jarvis.log")))

    # ---- window behaviour

    def showEvent(self, event):
        super().showEvent(event)
        if not self._titled:
            self._titled = True
            _dark_title_bar(self)

    def show_front(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()
        self.refresh_current_page()

    def closeEvent(self, event):
        if self.quitting:
            event.accept()
            return
        event.ignore()  # closing hides to the tray; quit from the tray menu
        self.hide()
        app = QApplication.instance()
        if not self._hint_shown and hasattr(app, "tray"):
            self._hint_shown = True
            app.tray.showMessage(settings.name, "Still running in the tray (hidden icons). Right-click the icon to quit.",
                                 hud_icon("sleeping"), 4000)


class DiagnosticsDialog(QDialog):
    """Runs the self-check (jarvis.doctor) in the background and lists the results."""

    MARK = {"ok": ("✓", TEAL), "warn": ("!", AMBER), "fail": ("✗", RED)}

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(f"{settings.name} diagnostics")
        self.resize(640, 460)
        self.setStyleSheet(f"QDialog {{ background: {BG}; }}")
        v = QVBoxLayout(self)
        v.addWidget(QLabel("DIAGNOSTICS", objectName="section"))
        self.view = QTextBrowser()
        v.addWidget(self.view, 1)
        close = QPushButton("CLOSE")
        close.clicked.connect(self.accept)
        v.addWidget(close, 0, Qt.AlignRight)
        self.lines: list[str] = []
        self.signals = Signals()
        self.signals.status.connect(self._progress)
        self.signals.notice.connect(self._result)
        threading.Thread(target=self._run, daemon=True).start()
        self._progress("Starting checks")

    def showEvent(self, event):
        super().showEvent(event)
        _dark_title_bar(self)

    def _run(self):
        from .doctor import run_all

        for c in run_all(progress=lambda name: self.signals.status.emit(name)):
            self.signals.notice.emit(c.status, f"{c.name}\t{c.detail}")
        self.signals.status.emit("")

    def _progress(self, name: str):
        tail = f'<p style="color:{DIM};">Checking {html.escape(name)}...</p>' if name else \
            f'<p style="color:{DIM};">Done.</p>'
        self.view.setHtml("".join(self.lines) + tail)

    def _result(self, status: str, text: str):
        mark, color = self.MARK.get(status, ("?", DIM))
        name, detail = text.split("\t", 1)
        self.lines.append(f'<p style="margin:6px 0;"><span style="color:{color}; font-weight:600;">{mark}</span>'
                          f'&nbsp; <b>{html.escape(name)}</b><br><span style="color:{DIM};">'
                          f'&nbsp;&nbsp;&nbsp; {html.escape(detail)}</span></p>')


# ---------------------------------------------------------------- app

class JarvisApp(QApplication):
    def __init__(self, argv, engine_factory=None):
        super().__init__(argv)
        self.setQuitOnLastWindowClosed(False)
        self.setStyleSheet(STYLE)
        self.setApplicationName(settings.name)
        self.setFont(QFont("Segoe UI", 10))
        self.server: QLocalServer | None = None

        self.signals = Signals()
        listener = QtListener(self.signals)
        self.engine = engine_factory(listener) if engine_factory else Engine(listener, speak_typed=settings.speak_typed)
        self.window = MainWindow(self.signals, self.engine)
        self._save_icon_file()
        self._build_tray()

    def _build_tray(self):
        from . import winstartup

        try:
            winstartup.migrate_renamed(ICON_FILE)
        except Exception:
            log.exception("couldn't migrate startup entry")
        self.tray = QSystemTrayIcon(hud_icon("loading"), self)
        self.tray.setToolTip(f"{settings.name}: starting up")
        menu = QMenu()
        act_open = QAction(f"Open {settings.name}", menu)
        act_open.triggered.connect(self.window.show_front)
        menu.addAction(act_open)
        menu.addSeparator()
        self.act_listen = QAction("Listening (microphone on)", menu, checkable=True, checked=self.engine.listening)
        self.act_listen.toggled.connect(self.window.set_listening)
        menu.addAction(self.act_listen)
        self.act_startup = QAction("Start with Windows", menu, checkable=True, checked=winstartup.is_startup_enabled())
        self.act_startup.toggled.connect(self.window.set_startup)
        menu.addAction(self.act_startup)
        menu.addSeparator()
        act_quit = QAction(f"Quit {settings.name}", menu)
        act_quit.triggered.connect(self.quit_jarvis)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self._menu = menu  # keep a reference so it isn't garbage-collected
        self.tray.activated.connect(self._tray_clicked)
        self.tray.show()

    def _tray_clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.window.isVisible() and self.window.isActiveWindow():
                self.window.hide()
            else:
                self.window.show_front()

    def _save_icon_file(self):
        """An .ico for the Start Menu shortcut."""
        try:
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            hud_icon("sleeping", 256).pixmap(256, 256).save(str(ICON_FILE), "ICO")
            for px in (192, 512):  # home-screen icons for the phone app, on a solid background
                pm = QPixmap(px, px)
                pm.fill(QColor(BG))
                painter = QPainter(pm)
                draw_hud(painter, px / 2, px / 2, px * 0.9, QColor(AMBER), t=1.2, detail=True)
                painter.end()
                pm.save(str(settings.data_dir / f"phone-icon-{px}.png"), "PNG")
        except Exception:
            log.exception("couldn't save icon")

    def claim_single_instance(self) -> bool:
        """True if we're the only copy. Otherwise ask the running one to show itself."""
        sock = QLocalSocket()
        sock.connectToServer(INSTANCE_NAME)
        if sock.waitForConnected(300):
            sock.write(b"show")
            sock.flush()
            sock.waitForBytesWritten(300)
            return False
        QLocalServer.removeServer(INSTANCE_NAME)  # clear a stale one left by a crash
        self.server = QLocalServer(self)
        self.server.newConnection.connect(lambda: (self.server.nextPendingConnection(), self.window.show_front()))
        self.server.listen(INSTANCE_NAME)
        return True

    def release_single_instance(self):
        if self.server:
            self.server.close()
            self.server = None

    def quit_jarvis(self):
        self.engine.stop()
        self.tray.hide()
        self.window.quitting = True
        self.window.close()
        self.quit()

    def restart(self):
        """Start a fresh copy (to apply settings), then quit this one."""
        self.release_single_instance()
        QProcess.startDetached(sys.executable, [str(ROOT / "jarvis.pyw"), "--restarted"])
        self.quit_jarvis()


def _setup_logging():
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(settings.data_dir / "jarvis.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    # force=True: a library imported earlier may already have configured logging.
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for noisy in ("httpx", "httpcore", "google_genai", "urllib3", "faster_whisper", "primp", "ddgs",
                  "trafilatura", "htmldate", "courlan", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Without a console (pythonw), stdout/stderr are None and libraries that print would crash.
    if sys.stdout is None or sys.stderr is None:
        console = settings.data_dir / "jarvis-console.log"
        if console.exists() and console.stat().st_size > 2_000_000:  # keep it from growing forever
            console.unlink()
        stream = open(console, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream


def main(argv=None, engine_factory=None):
    argv = argv if argv is not None else sys.argv
    _setup_logging()
    try:  # our own taskbar icon and grouping, instead of Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Jarvis.Assistant")
    except Exception:
        pass
    if "--restarted" in argv:
        time.sleep(1.5)  # let the old copy release the microphone and the Claude bridge port
    app = JarvisApp(argv, engine_factory)
    if not app.claim_single_instance():
        return 0
    if "--minimized" not in argv:
        app.window.show_front()
    app.engine.start()
    return app.exec()
