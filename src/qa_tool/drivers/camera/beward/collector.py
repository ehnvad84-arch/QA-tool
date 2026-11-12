"""Сборщик-заготовка Beward: адреса CGI не подтверждены (решение директора 09.11)."""

from qa_tool.drivers.camera.stub_collector import StubCollector

BEWARD = StubCollector(
    "beward",
    "заготовка",
    frozenset({"beward"}),
    "адреса родного API Beward не подтверждены: нужен доступ к камере в сети стенда "
    "или документ поставщика",
)
