"""Проверка собранной программы: ONVIF, API, поток и кодеки против виртуальных камер.

Запуск из корня репозитория (виртуальная камера — в tests/):
    uv run python packaging/smoke_onvif.py <программа> [аргументы до «run» …]
Например: `… build/pyinstaller/qa-tool/qa-tool-cli.exe` или `… dist/QA-tool.AppImage
--appimage-extract-and-run`. Без аргументов проверяется программа из исходников (`qa-tool`).

Проверяется то, что ломается именно в сборке: onvif-tt запускается из собранной программы
(без `python -m`), находит свои схемы и выдаёт строки протокола; свой RTSP-клиент читает поток
метаданных; карта API «Рувер» лежит в сборке, и проверка API выдаёт строки операций;
«API камеры» снимает карту виртуальных Hikvision и Dahua, сверяет её по таблице пар из сборки
и выпускает документ «Карта API»; проверка кодеков меняет кодировщик и возвращает его, а VMAF
считает libvmaf ffmpeg из сборки. Картинка проверяется отдельно: `smoke_image.py`.
Код выхода: 0 — сборка годна, 1 — нет.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.mock_camera import MockCamera, Passport, stand_yaml  # noqa: E402
from tests.mock_ruver import MockRuver  # noqa: E402

from qa_tool.core.store import RunStore  # noqa: E402

CHECKS = [
    "CORE-CONNECT-1",
    "ONVIF-TT",
    "RUV-S-STREAM-1",
    "RUV-M-METADATA-1",
    "RUV-M-STREAM-1",
    "RUV-M-ANALYTICS-1",
    "RUV-SD-1",
    "RUV-PROFILES-1",
]
MUST_PASS = ["CORE-CONNECT-1", "RUV-S-STREAM-1", "RUV-M-METADATA-1", "RUV-M-STREAM-1", "RUV-SD-1"]
MIN_OTT_ROWS = 30  # у закреплённой onvif-tt по виртуальной камере ~100 строк; 30 — «запустился»
STREAM_PASSWORD = "smoke-S3cret-pw"  # отличается от логина: поиск по файлам прогона честный
MIN_API_ROWS = 250  # в карте 300 операций; меньше — карта в сборке не та или обход сорвался


def run_app(
    app: list[str], folder: Path, password: str = "admin"
) -> dict[str, tuple[str, str]] | None:
    """Прогнать `stand.yaml` из папки; строки протокола {ID: (статус, причина)} или None."""
    env = {**os.environ, "QA_TOOL_PASSWORD_1": password}
    done = subprocess.run(
        [*app, "run", "--stand", str(folder / "stand.yaml")],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1200,
    )
    print(done.stdout[-4000:], done.stderr[-4000:], sep="\n")
    # рядом с прогонами лежат папки журналов записи (`pending-image`): прогон — только `QA-*`
    runs = sorted(p for p in (folder / "runs").glob("QA-*") if p.is_dir())
    if not runs:
        print("ПРОВАЛ: прогон не создал папку результатов")
        return None
    store = RunStore(runs[-1], readonly=True)
    rows = {r["check_id"]: (r["status"], r["reason"]) for r in store.results()}
    store.close()
    return rows


def smoke_api(app: list[str]) -> list[str]:
    """Проверка API «Рувер» в сборке против виртуальной камеры; список проблем."""
    cam = MockRuver().start()
    try:
        with tempfile.TemporaryDirectory(prefix="qa-tool-smoke-api-") as tmp:
            folder = Path(tmp)
            (folder / "stand.yaml").write_text(
                "place: color_box\nruns_dir: runs\ninterface: ''\ncameras:\n"
                f"- {{slot: 1, address: 127.0.0.1, port: {cam.port}, username: admin, "
                "vendor: ruver}\n",
                encoding="utf-8",
            )
            (folder / "scenario.yaml").write_text(
                "checks: [API-RUVER-1]\nallow_writes: false\n", encoding="utf-8"
            )
            rows = run_app(app, folder)
    finally:
        cam.stop()
    if rows is None:
        return ["API: прогон не создал папку результатов"]
    api_rows = [i for i in rows if i.startswith("API-")]
    print(f"строк API: {len(api_rows)}")
    return [] if len(api_rows) >= MIN_API_ROWS else [f"строк API {len(api_rows)} < {MIN_API_ROWS}"]


def smoke_api_map(app: list[str]) -> list[str]:
    """«API камеры» в сборке на виртуальных Hikvision и Dahua: карта, сверка по таблице пар из
    сборки (`qa_tool/data/api_pairs`), документ «Карта API» из шаблона сборки; список проблем."""
    from tests.mock_native import dahua, hikvision

    problems = []
    for maker, model, api in (
        ("Hikvision", "DS-2CD2043G2-I", hikvision()),
        ("Dahua", "IPC-HFW2431S-S-S2", dahua()),
    ):
        cam = MockCamera(Passport(maker, model, "1.0", "SN1"), native=api).start()
        try:
            with tempfile.TemporaryDirectory(prefix="qa-tool-smoke-api-map-") as tmp:
                folder = Path(tmp)
                (folder / "stand.yaml").write_text(stand_yaml([cam]), encoding="utf-8")
                (folder / "scenario.yaml").write_text(
                    "checks: [CORE-CONNECT-1, API-MAP-1]\nallow_writes: false\n",
                    encoding="utf-8",
                )
                rows = run_app(app, folder)
                runs = sorted(p for p in (folder / "runs").glob("QA-*") if p.is_dir())
                documents = [runs[-1] / f"api_map_1.{ext}" for ext in ("html", "pdf", "xlsx")]
                missing = [d.name for d in documents if not runs or not d.exists()]
                found = list(runs[-1].rglob("api-map.json")) if runs else []
                native = json.loads(found[0].read_text(encoding="utf-8")) if found else {}
        finally:
            cam.stop()
        if rows is None:
            problems.append(f"API камеры, {maker}: прогон не создал папку результатов")
            continue
        head = rows.get("API-MAP-1", ("нет строки", ""))
        if head[0] != "прошла":
            problems.append(f"API-MAP-1, {maker}: {head}")
        failed = [
            i for i, (status, _) in rows.items() if i.startswith("API-") and status == "не прошла"
        ]
        problems += [f"{i}, {maker}: {rows[i]}" for i in failed]
        problems += [f"«Карта API», {maker}: нет файла {name}" for name in missing]
        pairs = [i for i in rows if i.startswith(("API-MATCH-", "API-RANGE-"))]
        # у Hikvision в таблице программы две подтверждённые пары: без них данных сборки нет
        if maker == "Hikvision" and rows.get("API-MATCH-12", ("",))[0] != "прошла":
            problems.append(f"API-MATCH-12, {maker}: {rows.get('API-MATCH-12', ('нет строки',))}")
        sections = native.get("разделы", [])
        params = sum(len(x.get("параметры", [])) for x in sections)
        if not sections:
            problems.append(f"API камеры, {maker}: api-map.json пуст или не записан")
        print(
            f"API камеры, {maker}: {head[0]}; разделов {len(sections)}, параметров {params}, "
            f"строк сверки {len(pairs)}"
        )
    return problems


def smoke_stream(app: list[str]) -> list[str]:
    """Модуль «Поток» в сборке: нити, ffmpeg из сборки, UDP; пароля нет ни в одном файле прогона."""
    cam = MockCamera(Passport("Smoke", "M1", "1.0", "SN1"), password=STREAM_PASSWORD).start()
    os.environ["QA_TOOL_STREAM_SAMPLE_S"] = "3"
    try:
        with tempfile.TemporaryDirectory(prefix="qa-tool-smoke-stream-") as tmp:
            folder = Path(tmp)
            (folder / "stand.yaml").write_text(stand_yaml([cam]), encoding="utf-8")
            (folder / "scenario.yaml").write_text(
                "checks: [CORE-CONNECT-1, STREAM-PARAMS-1, STREAM-NET-1, STREAM-STRESS-1]\n"
                "allow_stress: true\nstress_minutes: 1\n",
                encoding="utf-8",
            )
            rows = run_app(app, folder, STREAM_PASSWORD)
            leaked = [
                str(path)
                for path in (folder / "runs").rglob("*")
                if path.is_file() and STREAM_PASSWORD.encode() in path.read_bytes()
            ]
    finally:
        cam.stop()
    if rows is None:
        return ["поток: прогон не создал папку результатов"]
    problems = [f"пароль в файле {path}" for path in leaked]
    for row in ("STREAM-PARAMS-1", "STREAM-DECODE-1", "STREAM-NET-1", "STREAM-STRESS-1"):
        if rows.get(row, ("",))[0] != "прошла":
            problems.append(f"{row}: {rows.get(row, ('нет строки', ''))}")
    print(f"строк потока: {sum(i.startswith('STREAM-') for i in rows)}")
    return problems


def smoke_codec(app: list[str]) -> list[str]:
    """Кодеки в сборке: запись кодировщика и возврат, VMAF на libvmaf ffmpeg сборки, PDF.

    Mock объявляет только H.264: H.265 для ролика mock нужен libx265 или kvazaar, а их есть не
    в каждом ffmpeg, из которого mock собирает ролик.
    """
    from tests.mock_onvif import MockStream, OnvifState, encoder_now

    state = OnvifState(encoder_video=True, encoder_codecs=("H264",))
    cam = MockCamera(Passport("Smoke", "M1", "1.0", "SN1"), state=state).start()
    fast = {
        "codec.apply_delay_s": 0.0,
        "codec.restart_wait_s": 1.0,
        "codec.clip_s": 2.0,
        "codec.clip_max_s": 2.0,
        "codec.frames": 15.0,
        "codec.min_frames": 10.0,
        "codec.grid_steps": 2.0,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="qa-tool-smoke-codec-") as tmp:
            folder = Path(tmp)
            (folder / "stand.yaml").write_text(stand_yaml([cam]), encoding="utf-8")
            (folder / "scenario.yaml").write_text(
                "checks: [CORE-CONNECT-1, CODEC-QUALITY-1]\nallow_writes: true\n",
                encoding="utf-8",
            )
            (folder / "thresholds.yaml").write_text(
                "".join(
                    f"{key}: {{value: {value}, unit: '', source: проверка сборки}}\n"
                    for key, value in fast.items()
                ),
                encoding="utf-8",
            )
            rows = run_app(app, folder)
            runs = sorted(p for p in (folder / "runs").glob("QA-*") if p.is_dir())
            pdf = runs[-1] / "protocol_1.pdf" if runs else None
            pdf_size = pdf.stat().st_size if pdf and pdf.exists() else 0
            charts = list(runs[-1].rglob("codec-curve-*.png")) if runs else []
    finally:
        cam.stop()
    if rows is None:
        return ["кодеки: прогон не создал папку результатов"]
    problems = []
    # «прошла» у строки кодека — значит, VMAF посчитан: без libvmaf строка «ошибка стенда»
    for row in ("CODEC-STATE-1", "CODEC-H264-1", "CODEC-RESTORE-1"):
        if rows.get(row, ("",))[0] != "прошла":
            problems.append(f"{row}: {rows.get(row, ('нет строки', ''))}")
    now = encoder_now(state, MockStream(state.profile_token))
    if (now["encoding"], now["kbps"], now["constant"]) != ("H264", 4000, False):
        problems.append(f"кодеки: кодировщик виртуальной камеры не вернулся: {now}")
    if not charts:
        problems.append("кодеки: график качества в протоколе не нарисован")
    if pdf_size < 50_000:  # без графика и снимков протокол — десятки килобайт
        problems.append(f"кодеки: PDF протокола {pdf_size} байт — графика и снимков в нём нет")
    print(f"строк кодеков: {sum(i.startswith('CODEC-') for i in rows)}, PDF {pdf_size} байт")
    return problems


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
            rows = run_app(app, folder)
            if rows is None:
                return 1
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
    problems += smoke_api(app)
    problems += smoke_api_map(app)
    problems += smoke_stream(app)
    problems += smoke_codec(app)
    for problem in problems:
        print("ПРОВАЛ:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    # консоль Windows в CI — cp1252: русский текст протокола иначе роняет печать
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:] or ["qa-tool"]))
