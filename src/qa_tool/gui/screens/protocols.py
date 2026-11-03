"""Экран «Протоколы»: прогоны из папки прогонов, протоколы камер и сводка в трёх форматах."""

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem

from qa_tool.core.scheduler import summary_status
from qa_tool.core.store import RunStore
from qa_tool.gui.help import helped
from qa_tool.gui.screens.base import Screen
from qa_tool.report.model import build_protocol

SUMMARY = "summary"


def open_file(path: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def list_runs(runs_dir: Path) -> list[dict]:
    runs = []
    for run_dir in sorted(runs_dir.glob("QA-*"), reverse=True):
        if not (run_dir / "run.sqlite").exists():
            continue
        store = RunStore(run_dir)
        try:
            info = store.run_info()
            worst = summary_status(store)
            cameras = [
                {
                    "slot": c["slot"],
                    "name": build_protocol(store, c["slot"]).camera_name,
                    "verdict": build_protocol(store, c["slot"]).verdict,
                }
                for c in store.cameras()
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
                self.docs_table.setItem(r, c, QTableWidgetItem(value))
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
