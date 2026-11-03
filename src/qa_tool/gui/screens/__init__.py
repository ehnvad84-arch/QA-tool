"""Пять экранов окна в порядке меню."""

from qa_tool.gui.screens.base import Screen
from qa_tool.gui.screens.cameras import CamerasScreen
from qa_tool.gui.screens.run import RunScreen
from qa_tool.gui.screens.scenario import ScenarioScreen
from qa_tool.gui.screens.stand import StandScreen


class ProtocolsScreen(Screen):
    title = "Протоколы"
    help_id = "protocols"
    hint = "Готовые протоколы прогонов: открыть PDF, Excel или HTML"
    intro = "Выберите прогон и откройте протокол камеры или сводный отчёт."


SCREENS = [StandScreen, CamerasScreen, ScenarioScreen, RunScreen, ProtocolsScreen]
