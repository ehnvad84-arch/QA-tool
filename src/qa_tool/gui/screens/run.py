"""Экран «Прогон», вариант D: список мест стенда и карточка выбранной камеры.

Слева (после колонки разделов) — места с процентом, итогом и цветной точкой (S1), справа —
шапка камеры, «Ход проверки» и «Задачи программистам» (S2–S4). Всё, что показывается,
собирает `RunView`; здесь только раскладка. Тексты из ответов камер (причины, паспорт) —
недоверенные: метки, подсказки и окошки с ними показывают простой текст, иначе Qt сам распознал
бы в них HTML (S11).
"""

from datetime import datetime
from html import escape
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from qa_tool.core.checks import Status
from qa_tool.core.models import MAX_SLOTS
from qa_tool.gui.help import helped
from qa_tool.gui.run_view import RUNNING, RunView, SlotView
from qa_tool.gui.screens.base import Screen
from qa_tool.gui.screens.protocols import open_file

STEP_TONES = {
    Status.PASSED.value: "ok",
    Status.FAILED.value: "failed",
    Status.SKIPPED.value: "idle",
    Status.STAND_ERROR.value: "stand",
    RUNNING: "running",
}
CARD_REFRESH_MS = 150  # onvif-tt шлёт сотни строк подряд: карточку перерисовываем пачкой


def set_prop(widget: QWidget, name: str, value) -> None:
    """Поменять свойство стиля и сразу перекрасить элемент (Qt сам этого не делает)."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def plain(text: str = "", role: str = "", wrap: bool = False) -> QLabel:
    """Метка с простым текстом: разметка из ответа камеры показывается как есть."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    if wrap:
        # Иначе ширину метки задаёт самое длинное слово (onvif_tt.runtime.raw_soap.…):
        # карточка вылезает за блок и появляется горизонтальная прокрутка.
        label.setMinimumWidth(1)
    if role:
        label.setProperty("role", role)
    return label


def safe_tip(text: str) -> str:
    """Подсказка из текста камеры: Qt сам распознаёт в подсказках HTML, поэтому экранируем."""
    return f"<span>{escape(text)}</span>" if text else ""


def plain_warning(parent: QWidget, title: str, text: str) -> None:
    """Предупреждение с текстом камеры: QMessageBox.warning тоже распознаёт в нём HTML."""
    box = QMessageBox(QMessageBox.Icon.Warning, title, text, parent=parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.exec()


def discard(widget: QWidget) -> None:
    """Убрать строку сразу: deleteLater удаляет её только в следующем проходе цикла событий,
    и до того старая строка просвечивает под новой."""
    widget.hide()
    widget.setParent(None)
    widget.deleteLater()


def dot(tone: str, size: str = "normal") -> QLabel:
    """Цветная точка состояния; цвет — из темы по свойству `tone`."""
    label = QLabel()
    label.setProperty("dot", size)
    label.setProperty("tone", tone)
    return label


class SlotRow(QWidget):
    """Строка места в списке камер: «Место N», производитель, точка, процент и итог."""

    def __init__(self, slot: int):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(5)
        top = QHBoxLayout()
        self.slot = plain(f"Место {slot}")
        self.slot.setStyleSheet("font-weight: bold;")
        self.maker = plain(role="muted")
        self.dot = dot("idle")
        top.addWidget(self.slot)
        top.addWidget(self.maker, 1)
        top.addWidget(self.dot)
        bottom = QHBoxLayout()
        self.percent = plain(role="muted")
        self.summary = plain(role="muted")
        bottom.addWidget(self.percent)
        bottom.addWidget(plain("·", role="muted"))
        bottom.addWidget(self.summary, 1)
        for label in (self.percent, self.summary):
            label.setStyleSheet("font-size: 12px;")
        layout.addLayout(top)
        layout.addLayout(bottom)

    def show_view(self, view: SlotView) -> None:
        """Показать состояние места."""
        self.maker.setText(view.maker)
        self.percent.setText(f"{view.percent} %")
        self.summary.setText(view.summary)
        self.summary.setToolTip(safe_tip(view.summary))  # длинная причина в строке обрезается
        set_prop(self.dot, "tone", view.dot.value)


class CameraList(QWidget):
    """Средняя колонка: места стенда, на которых стоят камеры (S1, S9)."""

    def __init__(self, open_cameras):
        super().__init__()
        self.setProperty("role", "cameras")
        self.setFixedWidth(330)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 18, 10, 10)
        head = QHBoxLayout()
        head.setContentsMargins(8, 0, 8, 6)
        title = plain("Камеры", role="h2")
        self.count = plain(role="muted")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.count)
        layout.addLayout(head)
        self.list = helped(
            QListWidget(),
            "Места стенда: процент выполнения, итог и цвет состояния. Синий — идёт, красный — "
            "есть ошибки, зелёный — без ошибок, жёлтый — ошибка стенда. Выберите место, "
            "чтобы увидеть его проверки справа.",
            "run-cameras",
        )
        self.list.setProperty("role", "camera-list")
        layout.addWidget(self.list, 1)
        self.empty = plain("Камер на местах нет", role="muted")
        self.empty_link = helped(
            QPushButton("Расставить камеры"),
            "Перейти в раздел «Камеры» и поставить камеры на места стенда.",
            "cameras",
        )
        self.empty_link.setProperty("role", "link")
        self.empty_link.clicked.connect(open_cameras)
        layout.addWidget(self.empty)
        layout.addWidget(self.empty_link)
        layout.addStretch(0)
        self.rows: dict[int, SlotRow] = {}

    def rebuild(self, view: RunView) -> None:
        """Строки по занятым местам; выбранное место сохраняется, если оно ещё занято."""
        selected = self.selected_slot()
        self.list.clear()
        self.rows = {}
        for slot, slot_view in view.slots.items():
            if slot_view.camera is None:
                continue
            row = SlotRow(slot)
            row.show_view(slot_view)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, slot)
            item.setSizeHint(QSize(300, 62))
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            self.rows[slot] = row
        self.count.setText(f"{len(self.rows)} из {MAX_SLOTS} на стенде")
        has = bool(self.rows)
        self.list.setVisible(has)
        self.empty.setVisible(not has)
        self.empty_link.setVisible(not has)
        if has:
            slots = list(self.rows)
            index = slots.index(selected) if selected in slots else 0
            self.list.setCurrentRow(index)

    def selected_slot(self) -> int | None:
        """Выбранное место или None."""
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class StepLog(QFrame):
    """Блок «Ход проверки» (S3): время, точка, название, состояние."""

    def __init__(self):
        super().__init__()
        self.setProperty("role", "panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.addWidget(plain("Ход проверки", role="h2"))
        self.scroll = helped(
            QScrollArea(),
            "Проверки выбранной камеры по порядку: когда началась, как закончилась. "
            "Набор тестов onvif-tt — одной строкой, итог по худшему тесту.",
            "run-steps",
        )
        self.scroll.setProperty("role", "plain")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.rows = QVBoxLayout(self.body)
        self.rows.setContentsMargins(0, 4, 0, 0)
        self.rows.setSpacing(9)
        self.rows.addStretch(1)
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)
        self.labels: list[tuple[QLabel, QLabel]] = []

    def show_view(self, view: SlotView) -> None:
        """Перерисовать ход проверки места."""
        at_bottom = self._at_bottom()
        while self.rows.count() > 1:
            discard(self.rows.takeAt(0).widget())
        self.labels = []
        for step in view.steps:
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(10)
            title = plain(step.label, wrap=True)
            if step.reason:
                title.setToolTip(safe_tip(step.reason))
            state = plain(step.state)
            state.setProperty("state", step.state)
            line.addWidget(plain(step.time or "—", role="time"))
            line.addWidget(dot(STEP_TONES.get(step.state, "idle"), "small"))
            line.addWidget(title, 1)
            line.addWidget(state)
            self.rows.insertWidget(self.rows.count() - 1, row)
            self.labels.append((title, state))
        if at_bottom:
            # новая строка должна быть видна без ручной прокрутки (S3)
            QTimer.singleShot(0, self._to_bottom)

    def _at_bottom(self) -> bool:
        bar = self.scroll.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _to_bottom(self) -> None:
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())


class TaskPanel(QFrame):
    """Блок «Задачи программистам» (S4, S8): карточки «не прошла» по важности."""

    def __init__(self, open_protocol):
        super().__init__()
        self.setProperty("role", "panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        head = QHBoxLayout()
        head.addWidget(plain("Задачи программистам", role="h2"))
        head.addStretch(1)
        self.protocol_link = helped(
            QPushButton("В протокол"),
            "Открыть протокол этой камеры. Доступно, когда прогон закончен и протоколы "
            "выпущены; во время прогона протокола ещё нет.",
            "run-tasks",
        )
        self.protocol_link.setProperty("role", "link")
        self.protocol_link.clicked.connect(open_protocol)
        head.addWidget(self.protocol_link)
        layout.addLayout(head)
        self.scroll = helped(
            QScrollArea(),
            "Ошибки камеры, которые уйдут программистам в таблицу задач протокола: сверху самые "
            "важные. Ошибки стенда и пропущенные проверки задачами не становятся.",
            "run-tasks",
        )
        self.scroll.setProperty("role", "plain")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.cards = QVBoxLayout(self.body)
        self.cards.setContentsMargins(0, 4, 0, 0)
        self.cards.setSpacing(10)
        self.cards.addStretch(1)
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)
        self.empty = plain("Задач нет: ни одна проверка не провалилась.", role="muted", wrap=True)
        layout.addWidget(self.empty)
        self.titles: list[str] = []

    def show_view(self, view: SlotView) -> None:
        """Перерисовать задачи места."""
        while self.cards.count() > 1:
            discard(self.cards.takeAt(0).widget())
        tasks = view.sorted_tasks()
        self.titles = [t.check_id for t in tasks]
        for task in tasks:
            card = QFrame()
            card.setProperty("role", "task")
            box = QVBoxLayout(card)
            box.setContentsMargins(12, 10, 12, 10)
            box.setSpacing(4)
            top = QHBoxLayout()
            severity = plain(task.severity)
            severity.setProperty("severity", task.severity)
            title = plain(task.title, wrap=True)
            title.setStyleSheet("font-weight: bold;")
            top.addWidget(severity, 0, Qt.AlignmentFlag.AlignTop)
            top.addWidget(title, 1)
            box.addLayout(top)
            reason = plain(task.reason or task.check_id, role="muted", wrap=True)
            reason.setToolTip(safe_tip(task.reason))
            reason.setStyleSheet("font-size: 13px;")
            box.addWidget(reason)
            self.cards.insertWidget(self.cards.count() - 1, card)
        self.empty.setVisible(not tasks)
        self.scroll.setVisible(bool(tasks))


class CameraCard(QWidget):
    """Карточка выбранной камеры (S2): шапка, ход проверки и задачи."""

    def __init__(self, open_protocol):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(18)
        head = QHBoxLayout()
        head.setSpacing(18)
        self.preview = helped(
            plain("превью потока — с этапа 4"),
            "Здесь будет живое видео камеры: модуль «Поток» (этап 4).",
            "run-card",
        )
        self.preview.setProperty("role", "preview")
        self.preview.setFixedSize(240, 135)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignTop)
        info = QVBoxLayout()
        info.setSpacing(6)
        self.place = plain(role="muted")
        self.model = plain(role="h1", wrap=True)
        self.details = plain(role="muted", wrap=True)
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self.chips = {}
        for status, chip in (
            (Status.PASSED, "passed"),
            (Status.FAILED, "failed"),
            (Status.SKIPPED, "skipped"),
            (Status.STAND_ERROR, "stand"),
        ):
            label = plain()
            label.setProperty("chip", chip)
            chips.addWidget(label)
            self.chips[status.value] = label
        chips.addStretch(1)
        for w in (self.place, self.model, self.details):
            info.addWidget(w)
        info.addLayout(chips)
        info.addStretch(1)
        head.addLayout(info, 1)
        layout.addLayout(head)
        panels = QHBoxLayout()
        panels.setSpacing(18)
        self.steps = StepLog()
        self.tasks = TaskPanel(open_protocol)
        panels.addWidget(self.steps, 1)
        panels.addWidget(self.tasks, 1)
        layout.addLayout(panels, 1)

    def show_view(self, view: SlotView | None) -> None:
        """Показать место; None — на стенде нет камер."""
        self.setVisible(view is not None)
        if view is None:
            return
        cam = view.camera
        self.place.setText(f"Место {view.slot}")
        self.model.setText(view.model)
        firmware = view.passport.get("firmware", "")
        address = cam.address if cam and view.model != cam.address else ""
        parts = [f"Прошивка {firmware}" if firmware else "", address]
        self.details.setText(" · ".join(p for p in parts if p))
        for status, label in self.chips.items():
            n = view.counts.get(status, 0)
            label.setText(f"{status} {n}")
            label.setVisible(status != Status.STAND_ERROR.value or n > 0)
        self.steps.show_view(view)
        self.tasks.show_view(view)


class RunScreen(Screen):
    title = "Прогон"
    help_id = "run"
    hint = "Ход прогона по каждой камере и задачи программистам"
    full_height = True

    def build(self) -> None:
        self.view = RunView(MAX_SLOTS)
        self.opener = open_file
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.cameras = CameraList(self._open_cameras)
        self.card = CameraCard(self.open_protocol)
        row.addWidget(self.cameras)
        row.addWidget(self.card, 1)
        self.layout_.addLayout(row)
        self.cameras.list.currentRowChanged.connect(lambda _row: self._show_card())
        self._card_timer = QTimer(self)
        self._card_timer.setSingleShot(True)
        self._card_timer.setInterval(CARD_REFRESH_MS)
        self._card_timer.timeout.connect(self._show_card)
        self.control = None
        self.showing_run = False  # на экране итог прогона, а не ожидание
        self.warn = plain_warning  # тесты подставляют запись вместо окошка
        self.refresh()

    def bind_run(self, control) -> None:
        """Подписаться на общий пульт прогона окна — до первого показа экрана."""
        self.control = control
        control.started.connect(self._on_started)
        control.event.connect(self._on_event)
        control.published.connect(self._on_published)
        control.failed.connect(self._on_failed)

    def refresh(self) -> None:
        if self.state.running:
            return
        if self.showing_run and self._same_cameras():
            # итог прогона остаётся на экране и после перехода в другой раздел
            self._update_protocol_link()
            return
        self.showing_run = False
        self._reset()

    def _same_cameras(self) -> bool:
        stand = self.state.stand
        return all(v.camera == stand.camera_at(n) for n, v in self.view.slots.items())

    def _reset(self) -> None:
        registry = self.state.registry()
        for slot in range(1, MAX_SLOTS + 1):
            cam = self.state.stand.camera_at(slot)
            total = (
                len(registry.plan_for(cam, self.state.scenario, self.state.stand.place))
                if cam
                else 0
            )
            self.view.reset(slot, cam, total)
        self.cameras.rebuild(self.view)
        self._show_card()

    def _on_started(self) -> None:
        # state.running уже True, поэтому не refresh(): новый прогон начинается с чистого экрана
        self.showing_run = True
        self._reset()
        self._update_protocol_link()

    def _on_event(self, event) -> None:
        slot = self.view.apply(event, datetime.now())
        if event.kind == "finished":
            for s, row in self.cameras.rows.items():
                row.show_view(self.view.slots[s])
            self._card_timer.start()
            return
        if slot is None or slot not in self.cameras.rows:
            return
        self.cameras.rows[slot].show_view(self.view.slots[slot])
        if slot == self.cameras.selected_slot() and not self._card_timer.isActive():
            self._card_timer.start()

    def _on_published(self, store, error: str) -> None:
        self._update_protocol_link()
        if error:
            self.warn(self, "Прогон", error)

    def _on_failed(self, error: str) -> None:
        self.showing_run = False
        self.warn(self, "Прогон", error)

    def _show_card(self) -> None:
        slot = self.cameras.selected_slot()
        self.card.show_view(self.view.slots[slot] if slot else None)
        self._update_protocol_link()

    def protocol_path(self) -> Path | None:
        """HTML-протокол выбранной камеры последнего прогона, если он выпущен."""
        store = self.control.last_store if self.control else None
        slot = self.cameras.selected_slot()
        if store is None or slot is None or self.state.running or not self.showing_run:
            return None
        path = store.run_dir / f"protocol_{slot}.html"
        return path if path.exists() else None

    def open_protocol(self) -> Path | None:
        """Открыть HTML-протокол выбранной камеры (S8)."""
        path = self.protocol_path()
        if path is not None:
            self.opener(path)
        return path

    def _update_protocol_link(self) -> None:
        self.card.tasks.protocol_link.setEnabled(self.protocol_path() is not None)

    def _open_cameras(self) -> None:
        window = self.window()
        titles = [s.title for s in window.screens]
        window.menu.setCurrentRow(titles.index("Камеры"))
