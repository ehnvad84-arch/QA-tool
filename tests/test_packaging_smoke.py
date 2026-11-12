"""Проверка сборки картинки (`packaging/smoke_image.py`) и её шагов в CI (этап 7, T20).

Сам смоук идёт в CI против собранной программы; здесь — его собственные решения, которые иначе
вскрылись бы только после пуша: какую папку считать прогоном и что считать провалом.
"""

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from qa_tool.checks.image.engine import RESTORE_ID, STATE_ID
from qa_tool.checks.image.settings import SETTINGS

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def smoke():
    spec = importlib.util.spec_from_file_location("smoke_image", ROOT / "packaging/smoke_image.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_folder_is_the_run_not_the_write_journal(smoke, tmp_path):
    # программа-заглушка создаёт то же, что настоящий прогон с записью: папку прогона и рядом
    # папку журнала `pending-image`, которая в сортировке идёт после `QA-…`
    fake = tmp_path / "fake_app.py"
    fake.write_text(
        textwrap.dedent(
            """
            from pathlib import Path
            for name in ("QA-20261110-001", "pending-image"):
                Path("runs", name).mkdir(parents=True)
            """
        ),
        encoding="utf-8",
    )
    folder = tmp_path / "work"
    folder.mkdir()
    (folder / "stand.yaml").write_text("cameras: []\n", encoding="utf-8")
    assert smoke.run_app([sys.executable, str(fake)], folder) == folder / "runs/QA-20261110-001"


def test_no_run_folder_is_reported_as_none(smoke, tmp_path):
    fake = tmp_path / "fake_app.py"
    fake.write_text("print('ошибка стенда')\n", encoding="utf-8")
    assert smoke.run_app([sys.executable, str(fake)], tmp_path) is None


def test_rows_must_cover_nine_settings_and_pass_the_main_ones(smoke):
    rows = {s.check_id: ("пропущена", "нужна сцена") for s in SETTINGS}
    rows |= {row: ("прошла", "") for row in (STATE_ID, "IMAGE-BRIGHT-1", RESTORE_ID)}
    assert smoke.check_rows(1, rows) == []
    del rows[SETTINGS[-1].check_id]
    assert [f"место 1: нет строки {SETTINGS[-1].check_id}"] == smoke.check_rows(1, rows)
    rows[RESTORE_ID] = ("не прошла", "настройка не вернулась")
    assert any(RESTORE_ID in problem for problem in smoke.check_rows(1, rows))


def test_files_without_snapshots_compare_or_chart_fail(smoke, tmp_path):
    run_dir = tmp_path / "runs" / "QA-20261110-001"
    (run_dir / "compare").mkdir(parents=True)
    problems = smoke.check_files(run_dir)
    assert any("image_compare.pdf" in p for p in problems)
    assert any(f"compare/{SETTINGS[0].check_id}.png" in p for p in problems)
    assert any("summary.html" in p for p in problems)


def test_complete_files_pass_and_password_in_any_file_fails(smoke, tmp_path):
    run_dir = tmp_path / "runs" / "QA-20261110-001"
    (run_dir / "compare").mkdir(parents=True)
    for name in ("protocol_1.pdf", "protocol_2.pdf", "image_compare.pdf"):
        (run_dir / name).write_bytes(b"x" * smoke.MIN_PDF_BYTES)
    for name in (
        "image_compare.html",
        "image_compare.xlsx",
        f"compare/{SETTINGS[0].check_id}.png",
    ):
        (run_dir / name).write_bytes(b"x" * smoke.MIN_CHART_BYTES)
    (run_dir / "summary.html").write_text('<a href="image_compare.html">', encoding="utf-8")
    assert smoke.check_files(run_dir) == []
    (run_dir / "evidence").mkdir()
    (run_dir / "evidence" / "onvif.xml").write_text(smoke.PASSWORD, encoding="utf-8")
    assert smoke.check_files(run_dir) == ["пароль камеры в файле onvif.xml"]


@pytest.mark.parametrize(
    ("job", "app"),
    [
        ("build-linux", '"$(ls dist/*.AppImage)" --appimage-extract-and-run'),
        ("build-windows", r"build\pyinstaller\qa-tool\qa-tool-cli.exe"),
    ],
)
def test_ci_runs_image_smoke_on_both_builds(job, app):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    commands = [step.get("run", "") for step in workflow["jobs"][job]["steps"]]
    assert f"uv run python packaging/smoke_image.py {app}" in commands
