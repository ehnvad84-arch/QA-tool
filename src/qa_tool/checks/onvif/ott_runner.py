"""Запускатель onvif-tt в отдельном процессе (вызывается из checks/onvif/onvif_tt.py).

Пароль камеры приходит переменной окружения QA_TOOL_OTT_PASSWORD, а не аргументом: у onvif-tt
есть только `--password`, и в командной строке дочернего процесса он был бы виден всем
пользователям компьютера. Поэтому запускатель вызывает `pytest.main` onvif-tt у себя в процессе.

Свой pytest.ini и каталог — пустые временные: иначе pytest нашёл бы pyproject.toml QA-tool
(где модуль onvif-tt отключён для наших тестов) по пути к пакету в .venv.

    python -m qa_tool.checks.onvif.ott_runner --target 127.0.0.1:PORT --user U --report R.json
        [--allow-writes] [--allow-reboot] [--exclude ID ...]
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

PASSWORD_ENV = "QA_TOOL_OTT_PASSWORD"


class Excluder:
    """Модуль pytest: убрать из прогона onvif-tt тесты из списка исключений до их запуска.

    `--deselect` здесь ненадёжен: ID узла зависит от пути к пакету onvif-tt относительно
    временного каталога, поэтому фильтр — по параметру `test_id` самого теста.
    """

    def __init__(self, test_ids: list[str]):
        self.test_ids = set(test_ids)

    def pytest_collection_modifyitems(self, config, items):
        """Отложить тесты из списка исключений; onvif-tt их не запустит."""
        keep, drop = [], []
        for item in items:
            params = getattr(getattr(item, "callspec", None), "params", {})
            (drop if params.get("test_id") in self.test_ids else keep).append(item)
        items[:] = keep
        if drop:
            config.hook.pytest_deselected(items=drop)


def build_pytest_args(args: argparse.Namespace, password: str, rootdir: Path) -> list[str]:
    """Аргументы pytest для прогона onvif-tt — как у `onvif-tt run`."""
    from onvif_tt.runner import dispatch

    ini = rootdir / "pytest.ini"
    ini.write_text("[pytest]\n", encoding="utf-8")
    pytest_args = [
        dispatch.__file__,
        "-c",
        str(ini),
        "--rootdir",
        str(rootdir),
        "-p",
        "no:cacheprovider",
        "-p",
        "onvif_tt.runner.plugin",
        "-q",
        f"--target={args.target}",
        f"--user={args.user}",
        f"--password={password}",
        f"--json-report={args.report}",
    ]
    if args.allow_writes:
        pytest_args.append("--allow-writes")
    if args.allow_reboot:
        pytest_args.append("--allow-reboot")
    return pytest_args


def main(argv: list[str] | None = None) -> int:
    """Прогнать onvif-tt; код возврата — как у pytest (0 — все прошли, 1 — есть упавшие)."""
    parser = argparse.ArgumentParser(prog="ott_runner")
    parser.add_argument("--target", required=True)
    parser.add_argument("--user", default="")
    parser.add_argument("--report", required=True)
    parser.add_argument("--allow-writes", action="store_true")
    parser.add_argument("--allow-reboot", action="store_true")
    parser.add_argument("--exclude", nargs="*", default=[])
    args = parser.parse_args(argv)
    password = os.environ.pop(PASSWORD_ENV, "")
    # только модуль onvif-tt: чужие модули pytest (например, pytest-qt) на компьютере стенда
    # без экрана роняли бы весь прогон
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    import pytest

    with tempfile.TemporaryDirectory(prefix="qa-tool-ott-") as rootdir:
        return int(
            pytest.main(
                build_pytest_args(args, password, Path(rootdir)), plugins=[Excluder(args.exclude)]
            )
        )


if __name__ == "__main__":
    sys.exit(main())
