from PySide6.QtGui import QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

from qa_tool.gui import theme
from qa_tool.gui.app import create_app
from qa_tool.gui.main_window import MainWindow
from qa_tool.gui.state import AppState

# Пары «текст — фон», которые реально встречаются в окне варианта D.
TEXT_PAIRS = [
    ("text", "window"),
    ("text", "nav"),
    ("text", "list"),
    ("text", "card"),
    ("muted", "window"),
    ("muted", "nav"),
    ("muted", "list"),
    ("muted", "card"),
    ("accent", "card"),
    ("accent", "window"),
    ("on_accent", "accent"),
    ("passed_fg", "passed_bg"),
    ("passed_fg", "card"),
    ("failed_fg", "failed_bg"),
    ("failed_fg", "card"),
    ("running_fg", "card"),
    ("skipped_fg", "skipped_bg"),
    ("skipped_fg", "card"),
    ("stand_fg", "stand_bg"),
    ("stand_fg", "card"),
]


def test_contrast_of_text_tokens_in_both_themes():
    weak = []
    for name, p in theme.THEMES.items():
        for fg, bg in TEXT_PAIRS:
            ratio = theme.contrast(getattr(p, fg), getattr(p, bg))
            if ratio < 4.5:
                weak.append(f"{name}: {fg} на {bg} = {ratio:.2f}")
    assert not weak, weak


def test_contrast_formula_reference_values():
    assert round(theme.contrast("#000000", "#ffffff"), 1) == 21.0
    assert round(theme.contrast("#777777", "#ffffff"), 2) == 4.48


def test_fonts_are_bundled_and_loaded():
    create_app()
    assert theme.font_files_missing() == []
    families = set(QFontDatabase.families())
    assert {theme.SANS, theme.MONO} <= families
    assert QApplication.instance().font().family() == theme.SANS


def test_theme_switch_from_stand_screen_repaints_and_is_remembered(tmp_path):
    app = create_app()
    theme.apply(app, "light")
    window = MainWindow(AppState(stand_path=tmp_path / "stand.yaml"))
    stand = window.screens[0]
    stand.theme.setCurrentIndex(stand.theme.findData("dark"))
    window_color = app.palette().color(QPalette.ColorRole.Window).name()
    assert window_color == theme.DARK.window
    assert theme.current() is theme.DARK and theme.saved_theme() == "dark"
    assert theme.DARK.accent in app.styleSheet()
    # новое окно (следующий запуск) открывается в сохранённой теме
    theme.apply(app, theme.saved_theme())
    again = MainWindow(AppState(stand_path=tmp_path / "stand.yaml"))
    assert again.screens[0].theme.currentData() == "dark"
    theme.apply(app, "light")


def test_first_launch_and_garbage_setting_give_light_theme():
    assert theme.saved_theme() == "light"
    theme.settings().setValue(theme.SETTINGS_KEY, "neon")
    assert theme.saved_theme() == "light"
