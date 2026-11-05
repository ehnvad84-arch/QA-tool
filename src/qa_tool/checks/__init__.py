"""Модули проверок. `load_all()` импортирует их, и они регистрируются в REGISTRY."""

import importlib

from qa_tool.core.checks import REGISTRY, Registry

MODULES = [
    "qa_tool.checks.core.connect",
    "qa_tool.checks.onvif.onvif_tt",
    "qa_tool.checks.onvif.replay",
    "qa_tool.checks.onvif.stream",
    "qa_tool.checks.onvif.metadata",
    "qa_tool.checks.onvif.storage",
    "qa_tool.checks.onvif.profiles",
    "qa_tool.checks.api.ruver",
    "qa_tool.checks.stream.params",
]


def load_all() -> None:
    """Импортировать все модули проверок, чтобы они зарегистрировались в REGISTRY."""
    for name in MODULES:
        importlib.import_module(name)


def registry() -> Registry:
    """Реестр со всеми модулями проверок; ссылка по умолчанию для планировщика."""
    load_all()
    return REGISTRY
