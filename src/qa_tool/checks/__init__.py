"""Модули проверок. `load_all()` импортирует их, и они регистрируются в REGISTRY."""

import importlib

MODULES = ["qa_tool.checks.core.connect"]


def load_all() -> None:
    for name in MODULES:
        importlib.import_module(name)
