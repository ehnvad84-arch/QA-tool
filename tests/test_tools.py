import os
import sys

from qa_tool import tools
from qa_tool.cli import main
from qa_tool.tools import tool_path


def test_bundled_ffmpeg_preferred_in_frozen_build(tmp_path, monkeypatch):
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    (tmp_path / "ffmpeg").mkdir()
    (tmp_path / "ffmpeg" / exe).write_text("")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert tool_path("ffmpeg") == str(tmp_path / "ffmpeg" / exe)


def test_doctor_reports_manual(capsys):
    main(["doctor"])
    out = capsys.readouterr().out
    assert "qa-tool" in out and "справка:" in out and "разделов" in out


def test_russian_output_survives_single_byte_console(monkeypatch):
    import io

    from qa_tool.cli import setup_console

    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252")  # как консоль Windows на раннере
    monkeypatch.setattr(sys, "stdout", console)
    setup_console()
    print("справка: разделов 21")
    console.flush()
    assert raw.getvalue().decode("utf-8").strip() == "справка: разделов 21"


def test_bundled_browser_is_used_only_in_a_build(tmp_path, monkeypatch):
    (tmp_path / "browser").mkdir()
    (tmp_path / "browser-libs").mkdir()
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    tools.use_bundled_browser()
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ  # не сборка: браузер разработчика
    assert tools.browser_env() is None
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    tools.use_bundled_browser()
    assert os.environ.pop("PLAYWRIGHT_BROWSERS_PATH") == str(tmp_path / "browser")
    assert tools.browser_env()["LD_LIBRARY_PATH"] == str(tmp_path / "browser-libs")
    assert "LD_LIBRARY_PATH" not in os.environ or os.environ["LD_LIBRARY_PATH"] != str(tmp_path)


def test_build_without_browser_libs_keeps_normal_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert tools.browser_env() is None
