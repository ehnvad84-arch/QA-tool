"""Точка входа собранной программы (PyInstaller).

`--ott-runner` первым аргументом — программа работает запускателем onvif-tt
(qa_tool.checks.onvif.ott_runner): в сборке нет `python -m`, поэтому проверка ONVIF-TT
запускает саму программу с этим флагом.
"""

import multiprocessing
import os
import sys

from qa_tool.checks.onvif.onvif_tt import RUNNER_FLAG

if __name__ == "__main__":
    multiprocessing.freeze_support()  # рабочие процессы камер в собранной программе
    if len(sys.argv) > 1 and sys.argv[1] == RUNNER_FLAG:
        from qa_tool.checks.onvif.ott_runner import main as ott_runner

        # у оконной сборки нет консоли: pytest нужен хоть какой-то stdout
        sys.stdout = sys.stdout or open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        sys.stderr = sys.stderr or sys.stdout
        raise SystemExit(ott_runner(sys.argv[2:]))
    from qa_tool.cli import main

    raise SystemExit(main())
