"""Экран «Протоколы»: прогоны из папки прогонов, протоколы камер и сводка в трёх форматах."""

import sqlite3
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem

from qa_tool.checks.image.settings import SETTINGS
from qa_tool.core.scheduler import summary_status
from qa_tool.core.store import RunStore
from qa_tool.gui.help import helped
from qa_tool.gui.screens.base import Screen
from qa_tool.report.model import build_protocol

SUMMARY = "summary"
IMAGE_COMPARE = "image_compare"
IMAGE_COMPARE_TIP = (
    "Снимки всех камер рядом в положениях 0, 50 и 100 % и график по каждой настройке."
)
CODEC_COMPARE = "codec_compare"
CODEC_COMPARE_TIP = "Кривые качества всех камер на одном графике по каждому разрешению и кодеку."
TIPS = {IMAGE_COMPARE: IMAGE_COMPARE_TIP, CODEC_COMPARE: CODEC_COMPARE_TIP}


def open_file(path: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def list_runs(runs_dir: Path) -> list[dict]:
    runs = []
    for run_dir in sorted(runs_dir.glob("QA-*"), reverse=True):
        if not (run_dir / "run.sqlite").exists():
            continue
        try:
            store = RunStore(run_dir, readonly=True)
        except sqlite3.Error:
            continue  # битый или чужой файл — остальные прогоны всё равно показываем
        try:
            info = store.run_info()
            worst = summary_status(store)
            protocols = [build_protocol(store, c["slot"]) for c in store.cameras()]
            cameras = [
                {"slot": p.slot, "name": p.camera_name, "verdict": p.verdict} for p in protocols
            ]
            runs.append(
                {
                    "dir": run_dir,
                    "id": store.run_id,
                    "started": info["started"],
                    "state": info["state"],
                    "result": worst.value if worst else "проверок нет",
                    "cameras": cameras,
                }
            )
        except (sqlite3.Error, KeyError, ValueError, TypeError):
            continue
        finally:
            store.close()
    return runs


class ProtocolsScreen(Screen):
    title = "Протоколы"
    help_id = "protocols"
    hint = "Готовые протоколы прогонов: открыть PDF, Excel или HTML"
    intro = "Выберите прогон, затем сводный отчёт или протокол камеры, и откройте нужный формат."

    def build(self) -> None:
        self.opener = open_file
        self.runs: list[dict] = []
        self.runs_table = helped(
            QTableWidget(0, 5),
            "Прогоны из папки прогонов, новые сверху. Выберите "
            "прогон, чтобы увидеть его протоколы.",
            "protocols-runs",
        )
        self.runs_table.setHorizontalHeaderLabels(
            ["Прогон", "Начат", "Камер", "Состояние", "Худший итог"]
        )
        self._setup(self.runs_table)
        self.runs_table.itemSelectionChanged.connect(self._show_documents)
        self.layout_.addWidget(self.runs_table)

        self.docs_table = helped(
            QTableWidget(0, 3),
            "Сводный отчёт прогона и протоколы камер. Выберите строку и откройте нужный формат.",
            "protocols-files",
        )
        self.docs_table.setHorizontalHeaderLabels(["Документ", "Камера", "Заключение"])
        self._setup(self.docs_table)
        self.layout_.addWidget(self.docs_table)

        row = QHBoxLayout()
        self.buttons = {}
        for fmt, text, tip in [
            ("pdf", "Открыть PDF", "Протокол для печати и подписи."),
            (
                "xlsx",
                "Открыть Excel",
                "Таблицы проверок и задач — удобно фильтровать и передавать.",
            ),
            ("html", "Открыть HTML", "Протокол в браузере."),
        ]:
            button = helped(QPushButton(text), tip, "protocols-files")
            button.clicked.connect(lambda _=False, f=fmt: self.open_selected(f))
            row.addWidget(button)
            self.buttons[fmt] = button
        self.folder_button = helped(
            QPushButton("Открыть папку прогона"),
            "Папка с базой, доказательствами и всеми протоколами прогона.",
            "protocols-files",
        )
        self.folder_button.clicked.connect(self.open_folder)
        row.addWidget(self.folder_button)
        row.addStretch(1)
        self.layout_.addLayout(row)
        self.empty = QLabel("")
        self.layout_.addWidget(self.empty)

    @staticmethod
    def _setup(table: QTableWidget) -> None:
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)

    def refresh(self) -> None:
        self.runs = list_runs(self.state.stand.runs_dir)
        self.runs_table.setRowCount(len(self.runs))
        for r, run in enumerate(self.runs):
            values = [
                run["id"],
                run["started"],
                str(len(run["cameras"])),
                run["state"],
                run["result"],
            ]
            for c, value in enumerate(values):
                self.runs_table.setItem(r, c, QTableWidgetItem(value))
        self.empty.setText(
            "" if self.runs else "Прогонов пока нет. Запустите прогон на экране «Прогон»."
        )
        if self.runs:
            self.runs_table.selectRow(0)
        else:
            self.docs_table.setRowCount(0)

    def selected_run(self) -> dict | None:
        rows = self.runs_table.selectionModel().selectedRows()
        return self.runs[rows[0].row()] if rows else None

    def _show_documents(self) -> None:
        run = self.selected_run()
        self.docs: list[str] = []
        if run is None:
            self.docs_table.setRowCount(0)
            return
        rows = [(SUMMARY, "Сводный отчёт", "все камеры", run["result"])]
        if (run["dir"] / f"{IMAGE_COMPARE}.html").exists():
            count = len(run["cameras"])
            rows.append(
                (
                    IMAGE_COMPARE,
                    "Сравнение картинки",
                    f"все камеры ({count})",
                    f"настроек {len(SETTINGS)}, камер {count}",
                )
            )
        if (run["dir"] / f"{CODEC_COMPARE}.html").exists():
            count = len(run["cameras"])
            # лист сравнения — на пару «разрешение · кодек», у каждой свой график
            pairs = len(list((run["dir"] / "compare").glob("codec_*.png")))
            rows.append(
                (
                    CODEC_COMPARE,
                    "Сравнение кодеков",
                    f"все камеры ({count})",
                    f"разрешений и кодеков {pairs}, камер {count}",
                )
            )
        rows += [
            (
                f"protocol_{c['slot']}",
                f"Протокол {run['id']}-{c['slot']}",
                f"{c['slot']}. {c['name']}",
                c["verdict"],
            )
            for c in run["cameras"]
        ]
        self.docs_table.setRowCount(len(rows))
        for r, (stem, *values) in enumerate(rows):
            self.docs.append(stem)
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                if stem in TIPS:
                    item.setToolTip(TIPS[stem])
                self.docs_table.setItem(r, c, item)
        self.docs_table.selectRow(0)

    def open_selected(self, fmt: str) -> Path | None:
        run = self.selected_run()
        rows = self.docs_table.selectionModel().selectedRows()
        if run is None or not rows:
            return None
        path = run["dir"] / f"{self.docs[rows[0].row()]}.{fmt}"
        if not path.exists():
            self.empty.setText(f"Файла нет: {path.name}")
            return None
        self.opener(path)
        return path

    def open_folder(self) -> None:
        run = self.selected_run()
        if run:
            self.opener(run["dir"])
