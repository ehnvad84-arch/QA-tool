"""HTML-протоколы: шаблоны Jinja2 с автоэкранированием (ответы камер — недоверенные данные)."""

from pathlib import Path

from jinja2 import Environment, PackageLoader

from qa_tool.report.model import Protocol, Summary

_ENV = Environment(loader=PackageLoader("qa_tool.report", "templates"), autoescape=True)


def render_protocol(protocol: Protocol) -> str:
    return _ENV.get_template("protocol.html").render(p=protocol)


def render_summary(summary: Summary) -> str:
    return _ENV.get_template("summary.html").render(s=summary)


def write_html(path: Path, html: str) -> Path:
    path.write_text(html, encoding="utf-8")
    return path
