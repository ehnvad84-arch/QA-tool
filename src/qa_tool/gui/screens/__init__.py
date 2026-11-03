"""Пять экранов окна в порядке меню."""

from qa_tool.gui.screens.cameras import CamerasScreen
from qa_tool.gui.screens.protocols import ProtocolsScreen
from qa_tool.gui.screens.run import RunScreen
from qa_tool.gui.screens.scenario import ScenarioScreen
from qa_tool.gui.screens.stand import StandScreen

SCREENS = [StandScreen, CamerasScreen, ScenarioScreen, RunScreen, ProtocolsScreen]
