"""Точка входа собранной программы (PyInstaller)."""

import multiprocessing

from qa_tool.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()  # рабочие процессы камер в собранной программе
    raise SystemExit(main())
