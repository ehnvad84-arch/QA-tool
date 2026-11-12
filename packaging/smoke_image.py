"""Проверка собранной программы: картинка двух виртуальных камер, протоколы и сравнение (S1, S10).

Запуск из корня репозитория:
    uv run python packaging/smoke_image.py <программа> [аргументы до «run» …]

Ломается именно в сборке: кадры снимает ffmpeg из сборки, величины считает numpy из неё же, а
графики сравнения и PDF со снимками рисует Qt без окна. Проверяется весь путь «снимок → запись
Imaging → возврат → протокол → сравнение камер»; пароль камеры не должен попасть ни в один файл
прогона. Пороги времени сокращены: виртуальный поток от настройки не зависит от выдержки.
Код выхода: 0 — сборка годна, 1 — нет.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.mock_camera import MockCamera, Passport, stand_yaml  # noqa: E402
from tests.mock_onvif import IMAGING_FULL, IMAGING_OPTIONS, OnvifState  # noqa: E402

from qa_tool.checks.image.engine import CHECK_ID, RESTORE_ID, STATE_ID  # noqa: E402
from qa_tool.checks.image.settings import SETTINGS  # noqa: E402
from qa_tool.core.store import RunStore  # noqa: E402

PASSWORD = "smoke-S3cret-pw"  # отличается от логина: поиск по файлам прогона честный
FAST = {"image.apply_delay_s": 0.5, "image.before_gap_s": 0.5, "image.clip_s": 1.0}
MUST_PASS = [STATE_ID, "IMAGE-BRIGHT-1", RESTORE_ID]
MIN_PDF_BYTES = 50_000  # без снимков протокол — десятки килобайт
MIN_CHART_BYTES = 2_000  # пустой холст PNG весит меньше
RUN_TIMEOUT_S = 1200


def run_app(app: list[str], folder: Path) -> Path | None:
    """Прогнать `stand.yaml` из папки; папка прогона `QA-…` или None, если её нет."""
    env = {**os.environ, "QA_TOOL_PASSWORD_1": PASSWORD, "QA_TOOL_PASSWORD_2": PASSWORD}
    started = time.monotonic()
    done = subprocess.run(
        [*app, "run", "--stand", str(folder / "stand.yaml")],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=RUN_TIMEOUT_S,
    )
    print(done.stdout[-4000:], done.stderr[-4000:], sep="\n")
    print(f"код выхода {done.returncode}, прогон {time.monotonic() - started:.0f} с")
    # рядом с прогонами лежат папки журналов записи (`pending-image`): прогон — только `QA-*`
    runs = sorted(p for p in (folder / "runs").glob("QA-*") if p.is_dir())
    return runs[-1] if runs else None


def slot_rows(run_dir: Path, slot: int) -> dict[str, tuple[str, str]]:
    """Строки протокола одной камеры {ID: (статус, причина)}."""
    store = RunStore(run_dir, readonly=True)
    try:
        return {r["check_id"]: (r["status"], r["reason"]) for r in store.results(slot=slot)}
    finally:
        store.close()


def check_rows(slot: int, rows: dict[str, tuple[str, str]]) -> list[str]:
    """Проблемы строк одной камеры: обязательные «прошла» и все 9 настроек на месте."""
    problems = [
        f"место {slot}, {row}: {rows.get(row, ('нет строки', ''))}"
        for row in MUST_PASS
        if rows.get(row, ("",))[0] != "прошла"
    ]
    problems += [
        f"место {slot}: нет строки {setting.check_id}"
        for setting in SETTINGS
        if setting.check_id not in rows
    ]
    return problems


def check_files(run_dir: Path) -> list[str]:
    """Проблемы файлов: протокол и сравнение со снимками, график, ссылка из сводки, пароль."""
    problems = []
    sizes = {
        name: (run_dir / name).stat().st_size if (run_dir / name).exists() else 0
        for name in (
            "protocol_1.pdf",
            "protocol_2.pdf",
            "image_compare.html",
            "image_compare.pdf",
            "image_compare.xlsx",
            f"compare/{SETTINGS[0].check_id}.png",
        )
    }
    print("файлы:", sizes)
    for name, size in sizes.items():
        floor = MIN_PDF_BYTES if name.endswith(".pdf") else MIN_CHART_BYTES
        if size < floor:
            problems.append(f"{name}: {size} байт (нужно не меньше {floor})")
    summary = run_dir / "summary.html"
    if not summary.exists() or "image_compare.html" not in summary.read_text(encoding="utf-8"):
        problems.append("в summary.html нет ссылки на сравнение картинки")
    leaked = [
        path.name
        for path in run_dir.parent.rglob("*")
        if path.is_file() and PASSWORD.encode() in path.read_bytes()
    ]
    problems += [f"пароль камеры в файле {name}" for name in leaked]
    return problems


def main(app: list[str]) -> int:
    """Прогнать `IMAGE-EFFECT-1` собранной программой на двух камерах и проверить результат."""
    if Path(app[0]).exists():  # прогон идёт во временной папке — относительный путь не найдётся
        app = [str(Path(app[0]).resolve()), *app[1:]]
    manual = IMAGING_FULL.replace("<tt:Exposure><tt:Mode>AUTO", "<tt:Exposure><tt:Mode>MANUAL")
    # у второй камеры другой диапазон яркости: сравнение идёт по процентам диапазона
    wide = IMAGING_OPTIONS.replace(
        "<tt:Brightness><tt:Min>0</tt:Min><tt:Max>100</tt:Max>",
        "<tt:Brightness><tt:Min>-50</tt:Min><tt:Max>200</tt:Max>",
    )
    states = [
        OnvifState(imaging_xml=manual, imaging_video=True),
        OnvifState(imaging_xml=manual, imaging_video=True, imaging_options_xml=wide),
    ]
    cameras = [
        MockCamera(
            Passport("Smoke", f"M{number}", "1.0", f"SN{number}"), password=PASSWORD, state=state
        ).start()
        for number, state in enumerate(states, start=1)
    ]
    problems = []
    try:
        with tempfile.TemporaryDirectory(prefix="qa-tool-smoke-image-") as tmp:
            folder = Path(tmp)
            (folder / "stand.yaml").write_text(stand_yaml(cameras), encoding="utf-8")
            (folder / "scenario.yaml").write_text(
                f"checks: [CORE-CONNECT-1, {CHECK_ID}]\nallow_writes: true\n", encoding="utf-8"
            )
            (folder / "thresholds.yaml").write_text(
                "".join(
                    f"{key}: {{value: {value}, unit: с, source: проверка сборки}}\n"
                    for key, value in FAST.items()
                ),
                encoding="utf-8",
            )
            run_dir = run_app(app, folder)
            if run_dir is None:
                print("ПРОВАЛ: прогон не создал папку результатов")
                return 1
            for slot in (1, 2):
                rows = slot_rows(run_dir, slot)
                print(f"место {slot}: строк картинки {sum(i.startswith('IMAGE-') for i in rows)}")
                problems += check_rows(slot, rows)
            problems += check_files(run_dir)
    finally:
        for camera in cameras:
            camera.stop()
    problems += [
        f"место {slot}: настройки виртуальной камеры не вернулись"
        for slot, state in enumerate(states, start=1)
        if state.imaging_xml != manual
    ]
    for problem in problems:
        print("ПРОВАЛ:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    # консоль Windows в CI — cp1252: русский текст протокола иначе роняет печать
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:] or ["qa-tool"]))
