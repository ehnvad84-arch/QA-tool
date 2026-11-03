"""Пять экранов окна в порядке меню."""

from qa_tool.gui.screens.base import Screen
from qa_tool.gui.screens.cameras import CamerasScreen
from qa_tool.gui.screens.stand import StandScreen


class ScenarioScreen(Screen):
    title = "Сценарий"
    help_id = "scenario"
    hint = "Какие проверки запускать и что им разрешено менять в камере"
    intro = (
        "Отметьте проверки. Изменяющие и разрушающие проверки выключены, пока вы их не разрешите."
    )


class RunScreen(Screen):
    title = "Прогон"
    help_id = "run"
    hint = "Запуск прогона и ход проверок по каждой камере"
    intro = "Запустите прогон и следите за проверками по каждому месту."


class ProtocolsScreen(Screen):
    title = "Протоколы"
    help_id = "protocols"
    hint = "Готовые протоколы прогонов: открыть PDF, Excel или HTML"
    intro = "Выберите прогон и откройте протокол камеры или сводный отчёт."


SCREENS = [StandScreen, CamerasScreen, ScenarioScreen, RunScreen, ProtocolsScreen]
