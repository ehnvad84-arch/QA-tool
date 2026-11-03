"""Модули проверок. `load_all()` импортирует их, и они регистрируются в REGISTRY."""

import importlib

from qa_tool.core.checks import REGISTRY, Registry

MODULES = ["qa_tool.checks.core.connect"]


def load_all() -> None:
    for name in MODULES:
        importlib.import_module(name)


def registry() -> Registry:
    """Реестр со всеми модулями проверок; ссылка по умолчанию для планировщика."""
    load_all()
    return REGISTRY
