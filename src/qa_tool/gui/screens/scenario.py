"""Экран «Сценарий»: дерево «модуль → группа → проверка», разрешения, длительность стресса."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QMessageBox,
    QTreeWidget,
    QTreeWidgetItem,
)

from qa_tool.checks.onvif.profiles import confirms
from qa_tool.core.checks import FIRST_CHECK, CheckSpec
from qa_tool.gui.help import HELP_ID, helped
from qa_tool.gui.screens.base import Screen
from qa_tool.report.model import MODULE_TITLES

# Решение директора 2026-11-03: длительность стресс-теста выбирается из списка шагом 10 минут;
# для долгих прогонов (ночь, сутки) — часы.
STRESS_CHOICES = [*range(10, 121, 10), 180, 240, 360, 480, 720, 1440]


GENERAL_HELP = "scenario-checks"


def check_tooltip(spec: CheckSpec) -> str:
    """Подсказка строки проверки: «что делаем, что ожидаем» и какие функции профилей ONVIF
    она подтверждает (та же матрица, что в разделе протокола «Профили ONVIF»)."""
    profiles = confirms(spec.id)
    if not profiles:
        return spec.description
    return f"{spec.description}\n\nПодтверждает профили ONVIF — {'; '.join(profiles)}."


def stress_label(minutes: int) -> str:
    """Подпись длительности: «40 мин», «3 ч», «24 ч»."""
    return f"{minutes // 60} ч" if minutes >= 180 and minutes % 60 == 0 else f"{minutes} мин"


class ScenarioScreen(Screen):
    title = "Сценарий"
    help_id = "scenario"
    hint = "Какие проверки запускать и что им разрешено менять в камере"
    intro = (
        "Отметьте проверки. Изменяющие и разрушающие проверки выключены, пока вы их не разрешите."
    )

    def build(self) -> None:
        self._loading = True
        self.tree = helped(
            QTreeWidget(),
            "Модули и проверки. Галочка — проверка войдёт в прогон. Серые "
            "проверки недоступны для камер на стенде — причина в подсказке строки.",
            "scenario-checks",
        )
        self.tree.setHeaderLabels(["Проверка", "Фаза", "Кому доступна"])
        self.tree.setColumnWidth(0, 420)
        self.tree.itemChanged.connect(self._changed)
        self.tree.currentItemChanged.connect(self._current_changed)
        self.layout_.addWidget(self.tree, 1)

        form = QFormLayout()
        self.allow_writes = helped(
            QCheckBox("Разрешить изменяющие проверки"),
            "Проверки, которые меняют "
            "настройки камеры. Настройки сохраняются до проверки и "
            "восстанавливаются после.",
            "scenario-permissions",
        )
        self.allow_destructive = helped(
            QCheckBox("Разрешить разрушающие проверки"),
            "Обновление прошивки и "
            "форматирование SD-карты. Сеть, логины и пароли камеры программа не меняет "
            "никогда, даже с этой галочкой.",
            "scenario-permissions",
        )
        self.stress = helped(
            QComboBox(),
            "Сколько держать максимальную нагрузку на все камеры в стресс-фазе: "
            "от 10 минут до суток.",
            "scenario-stress",
        )
        for minutes in STRESS_CHOICES:
            self.stress.addItem(stress_label(minutes), minutes)
        self.stress.setMaximumWidth(160)
        self.allow_writes.toggled.connect(self._changed)
        self.allow_destructive.toggled.connect(self._confirm_destructive)
        self.stress.currentIndexChanged.connect(self._changed)
        form.addRow(self.allow_writes)
        form.addRow(self.allow_destructive)
        form.addRow("Длительность стресс-теста", self.stress)
        self.layout_.addLayout(form)
        self.refresh()

    def _confirm_destructive(self, checked: bool) -> None:
        if checked and not self._loading:
            answer = QMessageBox.question(
                self,
                "Разрушающие проверки",
                "Разрушающие проверки могут обновить прошивку и стереть SD-карту. "
                "Сеть, логины и пароли камеры не меняются в любом случае. Разрешить?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.allow_destructive.setChecked(False)
                return
        self._changed()

    def _check_items(self) -> list[QTreeWidgetItem]:
        """Строки проверок (листья дерева) — модули и группы только собирают их."""
        items, stack = [], [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop(0)
            if item.data(0, Qt.ItemDataRole.UserRole):
                items.append(item)
            stack[:0] = [item.child(j) for j in range(item.childCount())]
        return items

    def _current_changed(self, current: QTreeWidgetItem | None, _previous=None) -> None:
        # F1 на дереве открывает раздел справки выбранной строки (проверки или группы)
        help_id = current.data(1, Qt.ItemDataRole.UserRole) if current else None
        self.tree.setProperty(HELP_ID, help_id or GENERAL_HELP)

    def _branch(self, parent, title: str, tooltip: str, help_id: str = "") -> QTreeWidgetItem:
        item = QTreeWidgetItem([title])
        item.setFlags(
            item.flags() | Qt.ItemFlag.ItemIsAutoTristate | Qt.ItemFlag.ItemIsUserCheckable
        )
        item.setToolTip(0, tooltip)
        item.setData(1, Qt.ItemDataRole.UserRole, help_id)
        if parent is None:
            self.tree.addTopLevelItem(item)
        else:
            parent.addChild(item)
        return item

    def _changed(self, *_args) -> None:
        if self._loading:
            return
        checked = [
            i.data(0, Qt.ItemDataRole.UserRole)
            for i in self._check_items()
            if i.checkState(0) == Qt.CheckState.Checked
        ]
        all_ids = [i.data(0, Qt.ItemDataRole.UserRole) for i in self._check_items()]
        scenario = self.state.scenario
        scenario.checks = None if checked == all_ids else checked
        scenario.allow_writes = self.allow_writes.isChecked()
        scenario.allow_destructive = self.allow_destructive.isChecked()
        scenario.stress_minutes = int(self.stress.currentData())
        self.state.save()

    def refresh(self) -> None:
        self._loading = True
        scenario, stand = self.state.scenario, self.state.stand
        self.tree.clear()
        modules: dict[str, QTreeWidgetItem] = {}
        groups: dict[tuple[str, str], QTreeWidgetItem] = {}
        for spec in self.state.registry().all():
            module = modules.get(spec.module)
            if module is None:
                title = MODULE_TITLES.get(spec.module, spec.module)
                module = self._branch(None, title, f"Все проверки модуля «{title}».")
                modules[spec.module] = module
            parent = module
            if spec.group:
                parent = groups.get((spec.module, spec.group))
                if parent is None:
                    parent = self._branch(
                        module, spec.group, f"Все проверки группы «{spec.group}».", spec.help_id
                    )
                    groups[(spec.module, spec.group)] = parent
            whom = "все производители" if spec.vendors is None else ", ".join(sorted(spec.vendors))
            item = QTreeWidgetItem([f"{spec.id} — {spec.title}", spec.phase.title, whom])
            item.setData(0, Qt.ItemDataRole.UserRole, spec.id)
            always = spec.id == FIRST_CHECK  # идёт в каждом прогоне: снять её нельзя
            flags = item.flags() | Qt.ItemFlag.ItemIsUserCheckable
            item.setFlags(flags & ~Qt.ItemFlag.ItemIsUserCheckable if always else flags)
            selected = always or scenario.checks is None or spec.id in scenario.checks
            item.setCheckState(0, Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
            item.setData(1, Qt.ItemDataRole.UserRole, spec.help_id)
            item.setToolTip(0, check_tooltip(spec))
            if stand.cameras and not any(spec.applies_to(c) for c in stand.cameras):
                item.setDisabled(True)
                item.setToolTip(
                    0, f"Нет камер этого производителя на стенде. {check_tooltip(spec)}"
                )
            parent.addChild(item)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)  # названия проверок длинные — без обрезки «…»
        self.allow_writes.setChecked(scenario.allow_writes)
        self.allow_destructive.setChecked(scenario.allow_destructive)
        index = self.stress.findData(scenario.stress_minutes)
        if index < 0:  # значение из scenario.yaml не из списка — показываем его как есть
            self.stress.addItem(stress_label(scenario.stress_minutes), scenario.stress_minutes)
            index = self.stress.count() - 1
        self.stress.setCurrentIndex(index)
        self._loading = False
