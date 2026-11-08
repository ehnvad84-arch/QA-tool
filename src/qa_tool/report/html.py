"""HTML-протоколы: шаблоны Jinja2 с автоэкранированием (ответы камер — недоверенные данные)."""

from pathlib import Path

from jinja2 import Environment, PackageLoader

from qa_tool.report.model import Protocol, Summary

_ENV = Environment(loader=PackageLoader("qa_tool.report", "templates"), autoescape=True)


def render_protocol(protocol: Protocol) -> str:
    """HTML протокола камеры (своя форма ОТК)."""
    return _ENV.get_template("protocol.html").render(p=protocol)


def render_summary(summary: Summary) -> str:
    """HTML сводного отчёта прогона."""
    return _ENV.get_template("summary.html").render(s=summary)


def render_template(name: str, **context) -> str:
    """HTML по шаблону из `templates/` с автоэкранированием."""
    return _ENV.get_template(name).render(**context)


def write_html(path: Path, html: str) -> Path:
    """Записать HTML в UTF-8."""
    path.write_text(html, encoding="utf-8")
    return path
