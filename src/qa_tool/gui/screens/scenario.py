"""Экран «Сценарий»: дерево модулей и проверок, разрешения, длительность стресса."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QMessageBox,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
)

from qa_tool.gui.help import helped
from qa_tool.gui.screens.base import Screen
from qa_tool.report.model import MODULE_TITLES


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
            QSpinBox(),
            "Сколько минут держать максимальную нагрузку в стресс-фазе.",
            "scenario-stress",
        )
        self.stress.setRange(1, 24 * 60)
        self.stress.setSuffix(" мин")
        self.stress.setMaximumWidth(140)
        self.allow_writes.toggled.connect(self._changed)
        self.allow_destructive.toggled.connect(self._confirm_destructive)
        self.stress.valueChanged.connect(self._changed)
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
        items = []
        for i in range(self.tree.topLevelItemCount()):
            module = self.tree.topLevelItem(i)
            items += [module.child(j) for j in range(module.childCount())]
        return items

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
        scenario.stress_minutes = self.stress.value()
        self.state.save()

    def refresh(self) -> None:
        self._loading = True
        scenario, stand = self.state.scenario, self.state.stand
        self.tree.clear()
        modules: dict[str, QTreeWidgetItem] = {}
        for spec in self.state.registry().all():
            module = modules.get(spec.module)
            if module is None:
                module = QTreeWidgetItem([MODULE_TITLES.get(spec.module, spec.module)])
                module.setFlags(
                    module.flags()
                    | Qt.ItemFlag.ItemIsAutoTristate
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                self.tree.addTopLevelItem(module)
                modules[spec.module] = module
            whom = "все производители" if spec.vendors is None else ", ".join(sorted(spec.vendors))
            item = QTreeWidgetItem([f"{spec.id} — {spec.title}", spec.phase.title, whom])
            item.setData(0, Qt.ItemDataRole.UserRole, spec.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            selected = scenario.checks is None or spec.id in scenario.checks
            item.setCheckState(0, Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
            item.setToolTip(0, spec.description)
            if stand.cameras and not any(spec.applies_to(c) for c in stand.cameras):
                item.setDisabled(True)
                item.setToolTip(0, f"Нет камер этого производителя на стенде. {spec.description}")
            module.addChild(item)
        self.tree.expandAll()
        self.allow_writes.setChecked(scenario.allow_writes)
        self.allow_destructive.setChecked(scenario.allow_destructive)
        self.stress.setValue(scenario.stress_minutes)
        self._loading = False
