"""Сборщики карты родного API всех брендов стенда: Hikvision, Dahua, «Рувер», Tiandy, Beward."""

from qa_tool.drivers.camera.beward.collector import BEWARD
from qa_tool.drivers.camera.dahua.collector import DahuaCollector
from qa_tool.drivers.camera.hikvision.collector import HikvisionCollector
from qa_tool.drivers.camera.native import Collector
from qa_tool.drivers.camera.ruver.collector import RuverCollector
from qa_tool.drivers.camera.tiandy.collector import TIANDY


def all_collectors() -> list[Collector]:
    """Новые сборщики в порядке выбора; у заготовок `reads()` пуст — карта по S2."""
    return [HikvisionCollector(), DahuaCollector(), RuverCollector(), TIANDY, BEWARD]
