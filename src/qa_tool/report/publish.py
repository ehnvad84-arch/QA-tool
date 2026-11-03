"""Выпуск протоколов прогона в папку прогона."""

from pathlib import Path

from qa_tool.core.store import RunStore
from qa_tool.report.html import render_protocol, render_summary, write_html
from qa_tool.report.model import build_summary


def publish(store: RunStore) -> list[Path]:
    summary = build_summary(store)
    written = []
    for protocol in summary.protocols:
        path = store.run_dir / f"protocol_{protocol.slot}.html"
        written.append(write_html(path, render_protocol(protocol)))
    written.append(write_html(store.run_dir / "summary.html", render_summary(summary)))
    return written
