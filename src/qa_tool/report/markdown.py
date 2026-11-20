"""Протоколы в Markdown: тот же протокол текстом — для задач, переписки и вставки в документы.

Ответы камер — недоверенные данные: каждое значение экранируется (`finalize`), чтобы ни
разметка, ни «|» из ответа камеры не ломали таблицы и не превращались в ссылки.
"""

import re
from pathlib import Path

from jinja2 import Environment, PackageLoader

from qa_tool.report.model import Protocol, Summary

_SPECIAL = re.compile(r"([\\`*_\[\]|#])")


def md_text(value) -> str:
    """Значение для ячейки или строки Markdown: спецсимволы экранированы, переносы — `<br>`."""
    if value is None:
        return ""
    text = _SPECIAL.sub(r"\\\1", str(value))
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return "<br>".join(line.strip() for line in text.splitlines()) or ""


_ENV = Environment(
    loader=PackageLoader("qa_tool.report", "templates"),
    autoescape=False,
    finalize=md_text,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


def render_protocol_md(protocol: Protocol) -> str:
    """Markdown протокола камеры: шапка, заключение, сводка, результаты, задачи."""
    return _ENV.get_template("protocol.md").render(p=protocol)


def render_summary_md(summary: Summary) -> str:
    """Markdown сводного отчёта прогона."""
    return _ENV.get_template("summary.md").render(s=summary)


def write_md(path: Path, text: str) -> Path:
    """Записать Markdown в UTF-8."""
    path.write_text(text, encoding="utf-8")
    return path
