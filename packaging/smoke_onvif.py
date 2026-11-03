"""Проверка собранной программы: модуль ONVIF против виртуальной камеры (T14).

Запуск из корня репозитория (виртуальная камера — в tests/):
    uv run python packaging/smoke_onvif.py <программа> [аргументы до «run» …]
Например: `… build/pyinstaller/qa-tool/qa-tool-cli.exe` или `… dist/QA-tool.AppImage
--appimage-extract-and-run`. Без аргументов проверяется программа из исходников (`qa-tool`).

Проверяется то, что ломается именно в сборке: onvif-tt запускается из собранной программы
(без `python -m`), находит свои схемы и выдаёт строки протокола; свой RTSP-клиент читает поток
метаданных. Код выхода: 0 — сборка годна, 1 — нет.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.mock_camera import MockCamera, Passport, stand_yaml  # noqa: E402

from qa_tool.core.store import RunStore  # noqa: E402

CHECKS = [
    "CORE-CONNECT-1",
    "ONVIF-TT",
    "RUV-M-METADATA-1",
    "RUV-M-STREAM-1",
    "RUV-M-ANALYTICS-1",
    "RUV-SD-1",
    "RUV-PROFILES-1",
]
MUST_PASS = ["CORE-CONNECT-1", "RUV-M-METADATA-1", "RUV-M-STREAM-1", "RUV-SD-1"]
MIN_OTT_ROWS = 30  # у закреплённой onvif-tt по виртуальной камере ~100 строк; 30 — «запустился»


def main(app: list[str]) -> int:
    """Прогнать сценарий собранной программой и проверить строки протокола."""
    if Path(app[0]).exists():  # прогон идёт во временной папке — относительный путь не найдётся
        app = [str(Path(app[0]).resolve()), *app[1:]]
    cam = MockCamera(Passport("Smoke", "M1", "1.0", "SN1")).start()
    try:
        with tempfile.TemporaryDirectory(prefix="qa-tool-smoke-") as tmp:
            folder = Path(tmp)
            stand = folder / "stand.yaml"
            stand.write_text(stand_yaml([cam]), encoding="utf-8")
            (folder / "scenario.yaml").write_text(
                "checks: [" + ", ".join(CHECKS) + "]\nallow_writes: false\n", encoding="utf-8"
            )
            env = {**os.environ, "QA_TOOL_PASSWORD_1": "admin"}
            done = subprocess.run(
                [*app, "run", "--stand", str(stand)],
                cwd=folder,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1200,
            )
            print(done.stdout[-4000:], done.stderr[-4000:], sep="\n")
            runs = sorted(p for p in (folder / "runs").glob("*") if p.is_dir())
            if not runs:
                print("ПРОВАЛ: прогон не создал папку результатов")
                return 1
            store = RunStore(runs[-1], readonly=True)
            rows = {r["check_id"]: (r["status"], r["reason"]) for r in store.results()}
            store.close()
    finally:
        cam.stop()
    problems = [
        f"{i}: {rows.get(i, ('нет строки', ''))}"
        for i in MUST_PASS
        if rows.get(i, ("",))[0] != "прошла"
    ]
    # строка ONVIF-TT сама в протокол не пишется — её заменяют строки тестов onvif-tt;
    # остаётся она только при сбое запуска (ошибка стенда)
    if "ONVIF-TT" in rows:
        problems.append(f"ONVIF-TT: {rows['ONVIF-TT']}")
    ott_rows = [i for i in rows if i not in CHECKS and not i.startswith(("RUV-", "CORE-"))]
    if len(ott_rows) < MIN_OTT_ROWS:
        problems.append(f"строк onvif-tt {len(ott_rows)} < {MIN_OTT_ROWS}")
    print(f"строк протокола: {len(rows)}, из них onvif-tt: {len(ott_rows)}")
    for problem in problems:
        print("ПРОВАЛ:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["qa-tool"]))
