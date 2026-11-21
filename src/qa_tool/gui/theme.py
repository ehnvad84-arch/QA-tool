"""Оформление окна в фирменном стиле «Рувер»: светлая и тёмная палитры, шрифты, логотип, выбор темы.

Фирменный стиль — брендбук «Рувер» (docs/work/brand-style/feature-spec.md): красный #BA0000 —
акцент и заголовки, чёрный, белый, серые #D8D8D8 и #F2F2F2 — фон и плашки, шрифт Montserrat.
Цвета задаются только здесь (токены из docs/work/gui-layout/feature-spec.md, раздел UI):
виджеты получают их через таблицу стилей по свойству `role` или берут `current()` при
рисовании. Тема меняется на ходу, без перезапуска, выбор запоминается в настройках
пользователя (INI-файл, а не реестр Windows: одинаково на обеих ОС и подменяется в тестах).
"""

from dataclasses import dataclass, fields
from pathlib import Path

from PySide6.QtCore import QRectF, QSettings, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QImage, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication

FONT_DIR = Path(__file__).parent / "fonts"
FONT_FILES = (
    "Montserrat-Regular.ttf",
    "Montserrat-Medium.ttf",
    "Montserrat-Bold.ttf",
    "PTMono-Regular.ttf",
)
SANS, MONO = "Montserrat", "PT Mono"
IMAGE_DIR = Path(__file__).parent / "images"
# Логотип и знак — из брендбука «Рувер»; для тёмной темы чёрные части логотипа белые
LOGO_FILES = {"light": "ruver-logo.png", "dark": "ruver-logo-dark.png"}
SIGN_FILE = "ruver-sign.png"
SETTINGS_KEY = "theme"


@dataclass(frozen=True)
class Palette:
    """Набор цветов одной темы. Пары «текст — фон» держат контраст не ниже 4.5:1 (тест)."""

    window: str
    nav: str
    list: str
    card: str
    border: str
    text: str
    muted: str
    accent: str
    on_accent: str
    # точки состояния мест (не текст: рядом всегда есть слово)
    dot_running: str
    dot_failed: str
    dot_ok: str
    dot_stand: str
    dot_idle: str
    # текст состояний и плашек: цвет текста, фон плашки
    passed_fg: str
    passed_bg: str
    failed_fg: str
    failed_bg: str
    running_fg: str
    skipped_fg: str
    skipped_bg: str
    stand_fg: str
    stand_bg: str
    preview: str
    preview_text: str


LIGHT = Palette(
    window="#f2f2f2",
    nav="#ffffff",
    list="#f7f7f7",
    card="#ffffff",
    border="#d8d8d8",
    text="#000000",
    muted="#555555",
    accent="#ba0000",
    on_accent="#ffffff",
    dot_running="#555555",
    dot_failed="#ba0000",
    dot_ok="#2e8b57",
    dot_stand="#c9871a",
    dot_idle="#b5b5b5",
    passed_fg="#23633a",
    passed_bg="#e4f0e6",
    failed_fg="#ba0000",
    failed_bg="#f9e0e0",
    running_fg="#333333",
    skipped_fg="#555555",
    skipped_bg="#e8e8e8",
    stand_fg="#7a4b0c",
    stand_bg="#f6ead3",
    preview="#1a1a1a",
    preview_text="#b5b5b5",
)

# Фирменный красный на тёмном фоне не читается (контраст 2,9), поэтому акцент светлее
DARK = Palette(
    window="#141414",
    nav="#0d0d0d",
    list="#191919",
    card="#1f1f1f",
    border="#333333",
    text="#f2f2f2",
    muted="#a8a8a8",
    accent="#ff5c5c",
    on_accent="#141414",
    dot_running="#bdbdbd",
    dot_failed="#ff5c5c",
    dot_ok="#4fb07a",
    dot_stand="#e0a440",
    dot_idle="#6e6e6e",
    passed_fg="#7fd19f",
    passed_bg="#1f3a2a",
    failed_fg="#ff9a92",
    failed_bg="#4a1c1c",
    running_fg="#d8d8d8",
    skipped_fg="#cfcfcf",
    skipped_bg="#333333",
    stand_fg="#f0cd8a",
    stand_bg="#4a3a1c",
    preview="#0a0a0a",
    preview_text="#a8a8a8",
)

THEMES = {"light": LIGHT, "dark": DARK}
TITLES = {"light": "светлая", "dark": "тёмная"}

_current = "light"


def settings() -> QSettings:
    """Настройки пользователя программы (тема окна)."""
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "QA-tool", "qa-tool")


def saved_theme() -> str:
    """Тема из настроек; при первом запуске и неизвестном значении — светлая."""
    name = settings().value(SETTINGS_KEY, "light")
    return name if name in THEMES else "light"


def current_name() -> str:
    """Имя темы, которая сейчас на окне."""
    return _current


def current() -> Palette:
    """Цвета темы, которая сейчас на окне: для виджетов, которые рисуют себя сами."""
    return THEMES[_current]


def logo() -> QPixmap:
    """Логотип «Рувер» для текущей темы."""
    return QPixmap(str(IMAGE_DIR / LOGO_FILES[_current]))


def draw_icon(size: int = 256) -> QImage:
    """Значок программы: фирменный знак «<Р>» на светлом скруглённом квадрате.

    Знак вытянут в ширину (≈3:1), поэтому лежит на подложке: без неё в панели задач
    он был бы тонкой полоской.
    """
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setPen(QColor(LIGHT.border))
    painter.setBrush(QColor(LIGHT.window))
    margin = size * 0.03
    painter.drawRoundedRect(
        QRectF(margin, margin, size - 2 * margin, size - 2 * margin), size * 0.18, size * 0.18
    )
    sign = QImage(str(IMAGE_DIR / SIGN_FILE))
    width = size * 0.8
    height = width * sign.height() / sign.width()
    painter.drawImage(QRectF((size - width) / 2, (size - height) / 2, width, height), sign)
    painter.end()
    return image


def app_icon() -> QIcon:
    """Значок окна и панели задач."""
    return QIcon(QPixmap.fromImage(draw_icon()))


def load_fonts() -> list[str]:
    """Подключить встроенные шрифты; вернуть семейства, которые Qt принял.

    Шрифт, который не загрузился, не роняет окно: Qt подставит системный, а `doctor`
    и тесты сообщат об этом.
    """
    families: list[str] = []
    for name in FONT_FILES:
        font_id = QFontDatabase.addApplicationFont(str(FONT_DIR / name))
        if font_id >= 0:
            families += QFontDatabase.applicationFontFamilies(font_id)
    return sorted(set(families))


def font_files_missing() -> list[str]:
    """Файлы шрифтов, которых нет в установке (для `qa-tool doctor` без окна)."""
    return [name for name in FONT_FILES if not (FONT_DIR / name).is_file()]


def apply(app: QApplication, name: str, remember: bool = False) -> Palette:
    """Перекрасить всё окно в тему `name` («light» или «dark») без перезапуска.

    Args:
        app: приложение Qt.
        name: имя темы из `THEMES`.
        remember: сохранить выбор до следующего запуска.

    Raises:
        KeyError: неизвестная тема.
    """
    global _current
    p = THEMES[name]
    _current = name
    app.setPalette(_qpalette(p))
    font = QFont(SANS)
    # Montserrat шире PT Sans, под который раскладывали окно, поэтому кегль на пиксель меньше
    font.setPixelSize(13)
    app.setFont(font)
    app.setStyleSheet(stylesheet(p))
    if remember:
        settings().setValue(SETTINGS_KEY, name)
    return p


def _qpalette(p: Palette) -> QPalette:
    # Палитра нужна стандартным виджетам (таблицы, диалоги, поля): таблица стилей задаёт
    # только наши роли, а остальное Qt рисует по палитре.
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: p.window,
        QPalette.ColorRole.WindowText: p.text,
        QPalette.ColorRole.Base: p.card,
        QPalette.ColorRole.AlternateBase: p.list,
        QPalette.ColorRole.Text: p.text,
        QPalette.ColorRole.Button: p.card,
        QPalette.ColorRole.ButtonText: p.text,
        QPalette.ColorRole.Highlight: p.accent,
        QPalette.ColorRole.HighlightedText: p.on_accent,
        QPalette.ColorRole.ToolTipBase: p.card,
        QPalette.ColorRole.ToolTipText: p.text,
        QPalette.ColorRole.PlaceholderText: p.muted,
        QPalette.ColorRole.Link: p.accent,
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(p.muted))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(p.muted))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(p.muted))
    return pal


def stylesheet(p: Palette) -> str:
    """Таблица стилей Qt для ролей окна варианта D (свойство `role` у виджета)."""
    return f"""
QMainWindow, QWidget[role="page"] {{ background: {p.window}; }}
QWidget[role="nav"] {{ background: {p.nav}; }}
QLabel[role="brand"] {{
    color: {p.muted}; font-size: 12px; font-weight: 500; padding: 4px 2px 14px 2px; }}
QListWidget[role="nav-list"] {{ background: transparent; border: none; outline: none; }}
QListWidget[role="nav-list"]::item {{ padding: 0 12px; border-radius: 8px; margin: 1px 0; }}
QListWidget[role="nav-list"]::item:selected {{
    background: {p.accent}; color: {p.on_accent}; font-weight: bold; }}
QListWidget[role="nav-list"]::item:hover:!selected {{ background: {p.window}; }}
QFrame[role="run-block"] {{ background: {p.window}; border-radius: 8px; }}
QWidget[role="cameras"] {{ background: {p.list}; border-right: 1px solid {p.border}; }}
QFrame[role="panel"] {{
    background: {p.card}; border: 1px solid {p.border}; border-radius: 12px; }}
QFrame[role="task"] {{
    background: {p.list}; border: 1px solid {p.border}; border-radius: 8px; }}
QLabel[role="muted"] {{ color: {p.muted}; }}
QLabel[role="progress"] {{ color: {p.muted}; font-size: 12px; }}
QLabel[role="notice"] {{
    background: {p.stand_bg}; border-radius: 8px; padding: 8px 12px; font-weight: bold; }}
QLabel[role="h1"] {{ color: {p.accent}; font-size: 22px; font-weight: bold; }}
QLabel[role="h2"] {{ font-size: 15px; font-weight: bold; }}
QLabel[role="time"] {{ font-family: "{MONO}"; font-size: 12px; color: {p.muted}; }}
QLabel[role="preview"] {{
    background: {p.preview}; color: {p.preview_text}; border-radius: 10px; font-size: 12px; }}
QLabel[chip="passed"] {{ background: {p.passed_bg}; color: {p.passed_fg}; }}
QLabel[chip="failed"] {{ background: {p.failed_bg}; color: {p.failed_fg}; }}
QLabel[chip="skipped"] {{ background: {p.skipped_bg}; color: {p.skipped_fg}; }}
QLabel[chip="stand"] {{ background: {p.stand_bg}; color: {p.stand_fg}; }}
QLabel[chip] {{ padding: 4px 10px; border-radius: 11px; }}
QLabel[severity="блокер"] {{ background: {p.failed_bg}; color: {p.failed_fg}; }}
QLabel[severity="серьёзная"] {{ background: {p.stand_bg}; color: {p.stand_fg}; }}
QLabel[severity="мелкая"] {{ background: {p.skipped_bg}; color: {p.skipped_fg}; }}
QLabel[severity] {{ padding: 1px 8px; border-radius: 4px; font-size: 12px; }}
QLabel[state="прошла"] {{ color: {p.passed_fg}; }}
QLabel[state="не прошла"] {{ color: {p.failed_fg}; }}
QLabel[state="идёт"] {{ color: {p.running_fg}; }}
QLabel[state="пропущена"] {{ color: {p.skipped_fg}; }}
QLabel[state="ошибка стенда"] {{ color: {p.stand_fg}; }}
QLabel[state] {{ font-size: 12px; }}
QPushButton {{
    background: {p.card}; border: 1px solid {p.border}; border-radius: 8px; padding: 7px 14px; }}
QPushButton:hover {{ border-color: {p.accent}; }}
QPushButton:disabled {{ color: {p.muted}; }}
QPushButton[role="primary"] {{
    background: {p.accent}; color: {p.on_accent}; border: none; font-weight: bold; }}
QPushButton[role="primary"]:disabled {{ background: {p.skipped_bg}; color: {p.muted}; }}
QPushButton[role="link"] {{
    background: transparent; border: none; color: {p.accent}; text-decoration: underline;
    padding: 0; text-align: left; }}
QPushButton[role="link"]:disabled {{ color: {p.muted}; text-decoration: none; }}
QListWidget[role="camera-list"] {{ background: transparent; border: none; outline: none; }}
QListWidget[role="camera-list"]::item {{
    border: 1px solid transparent; border-radius: 10px; margin: 2px 0; }}
QListWidget[role="camera-list"]::item:selected {{
    background: {p.card}; border: 1px solid {p.accent}; }}
QLabel[dot] {{ border-radius: 5px; min-width: 10px; max-width: 10px;
    min-height: 10px; max-height: 10px; }}
QLabel[dot="small"] {{ border-radius: 4px; min-width: 8px; max-width: 8px;
    min-height: 8px; max-height: 8px; }}
QLabel[tone="running"] {{ background: {p.dot_running}; }}
QLabel[tone="failed"] {{ background: {p.dot_failed}; }}
QLabel[tone="ok"] {{ background: {p.dot_ok}; }}
QLabel[tone="stand"] {{ background: {p.dot_stand}; }}
QLabel[tone="idle"] {{ background: {p.dot_idle}; }}
QScrollArea[role="plain"], QScrollArea[role="plain"] > QWidget > QWidget {{
    background: transparent; border: none; }}
QStatusBar {{ background: {p.nav}; color: {p.muted}; }}
QStatusBar QLabel {{ color: {p.muted}; font-size: 12px; padding: 2px 12px; }}
QToolTip {{ background: {p.card}; color: {p.text}; border: 1px solid {p.border}; }}
"""


def color_names() -> list[str]:
    """Имена всех токенов палитры (для тестов полноты тёмной темы)."""
    return [f.name for f in fields(Palette)]


def contrast(a: str, b: str) -> float:
    """Контраст двух цветов по WCAG 2.1 (1–21); текст требует не меньше 4.5."""

    def luminance(color: str) -> float:
        c = QColor(color)
        channels = []
        for v in (c.redF(), c.greenF(), c.blueF()):
            channels.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
        r, g, b_ = channels
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_

    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)
