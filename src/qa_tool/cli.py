"""Командная строка: `qa-tool` (окно), `qa-tool run`, `qa-tool discover`.

Коды выхода `run`: 0 — всё прошло, 1 — есть «не прошла», 2 — ошибка стенда или конфигурации.
"""

import argparse
import sys
from pathlib import Path

from qa_tool import __version__
from qa_tool.core.models import ConfigError

EXIT_OK, EXIT_FAILED, EXIT_STAND = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qa-tool", description="Стенд проверки IP-камер")
    parser.add_argument("--version", action="version", version=f"qa-tool {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="прогон без окна")
    run.add_argument("--stand", type=Path, required=True, help="файл стенда stand.yaml")
    run.add_argument("--scenario", type=Path, help="сценарий (по умолчанию scenario.yaml рядом)")
    run.add_argument("--thresholds", type=Path, help="пороги (по умолчанию thresholds.yaml рядом)")

    sub.add_parser("doctor", help="проверить установку: версия, ffmpeg, справка")

    discover = sub.add_parser("discover", help="найти камеры в сети")
    discover.add_argument("--interface", default="", help="IP сетевой карты к камерам")
    discover.add_argument("--target", help="адрес для прямого запроса")
    discover.add_argument("--timeout", type=float, default=3.0)
    return parser


def _beside(stand: Path, given: Path | None, name: str) -> Path | None:
    if given is not None:
        return given
    candidate = stand.parent / name
    return candidate if candidate.exists() else None


def cmd_run(args: argparse.Namespace) -> int:
    from qa_tool.core.checks import Status
    from qa_tool.core.config import load_scenario, load_stand, load_thresholds
    from qa_tool.core.scheduler import Runner, summary_status

    try:
        stand = load_stand(args.stand)
        scenario = load_scenario(_beside(args.stand, args.scenario, "scenario.yaml"))
        thresholds_path = _beside(args.stand, args.thresholds, "thresholds.yaml")
        thresholds = load_thresholds(thresholds_path) if thresholds_path else {}
        if not stand.cameras:
            raise ConfigError(f"{args.stand.name}: на стенде нет камер")

        def show(event):
            if event.kind == "phase":
                print(f"— фаза: {event.title}")
            elif event.kind == "result":
                tail = f" — {event.detail}" if event.detail else ""
                print(f"  место {event.slot}: {event.check_id} {event.status}{tail}")
            elif event.kind == "lost":
                print(f"  место {event.slot}: {event.detail}")

        store = Runner(stand, scenario, thresholds, on_event=show).run()
    except ConfigError as e:
        print(f"Ошибка конфигурации: {e}", file=sys.stderr)
        return EXIT_STAND
    _publish(store)
    worst = summary_status(store)
    print(f"Прогон {store.run_id}: {worst.value if worst else 'проверок нет'} → {store.run_dir}")
    return {Status.STAND_ERROR: EXIT_STAND, Status.FAILED: EXIT_FAILED}.get(worst, EXIT_OK)


def _publish(store) -> None:
    from qa_tool.report.publish import publish

    for path in publish(store):
        print(f"  протокол: {path.name}")


def cmd_discover(args: argparse.Namespace) -> int:
    from qa_tool.drivers.camera.discovery import discover

    found = discover(timeout=args.timeout, target=args.target, interface=args.interface)
    for cam in found:
        print(f"{cam.address}:{cam.port}\t{cam.manufacturer}\t{cam.model}")
    print(f"Найдено камер: {len(found)}")
    return EXIT_OK


def cmd_doctor() -> int:
    import subprocess

    from qa_tool.gui.help_window import load_topics, manual_dir
    from qa_tool.tools import tool_path

    print(f"qa-tool {__version__}")
    ok = True
    for name in ("ffmpeg", "ffprobe"):
        path = tool_path(name)
        if path is None:
            print(f"{name}: не найден")
            ok = False
            continue
        first = subprocess.run([path, "-version"], capture_output=True, text=True).stdout
        print(f"{name}: {path} — {first.splitlines()[0] if first else 'нет ответа'}")
    topics = load_topics()
    print(f"справка: {manual_dir()} — разделов {len(topics)}")
    ok = ok and "index" in topics
    return EXIT_OK if ok else EXIT_STAND


def setup_console() -> None:
    """Русский текст в консоли и в перенаправленном выводе на любой ОС.

    Консоль Windows по умолчанию в однобайтовой кодировке (cp1252, cp866): без этого
    первое же русское слово роняет программу (UnicodeEncodeError).
    """
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "discover":
        return cmd_discover(args)
    if args.command == "doctor":
        return cmd_doctor()
    from qa_tool.gui.app import run_gui

    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
