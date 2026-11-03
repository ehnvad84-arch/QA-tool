"""Справка по F1: разделы руководства из docs/manual/*.md, работает без интернета.

Раздел отмечен в Markdown строкой `<!-- help: <help_id> -->` перед заголовком.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QDialog, QTextBrowser, QVBoxLayout

_MARK = re.compile(r"<!--\s*help:\s*([\w-]+)\s*-->\s*\n#+\s*(.+)")


def manual_dir() -> Path:
    """В сборке — рядом с программой, при разработке — docs/manual репозитория."""
    bundled = Path(getattr(sys, "_MEIPASS", "")) / "manual"
    if bundled.is_dir():
        return bundled
    installed = Path(__file__).resolve().parents[1] / "manual"
    if installed.is_dir():
        return installed
    return Path(__file__).resolve().parents[3] / "docs" / "manual"


@dataclass(frozen=True)
class Topic:
    help_id: str
    file: Path
    heading: str


def load_topics(folder: Path | None = None) -> dict[str, Topic]:
    topics = {}
    for path in sorted((folder or manual_dir()).glob("*.md")):
        for help_id, heading in _MARK.findall(path.read_text(encoding="utf-8")):
            topics[help_id] = Topic(help_id, path, heading.strip())
    return topics


class HelpWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Справка QA-tool")
        self.resize(760, 640)
        self.setProperty("help_id", "index")
        self.browser = QTextBrowser()
        self.browser.setToolTip("Справка. Закрыть — Esc.")
        self.browser.setProperty("help_id", "index")
        self.browser.setOpenExternalLinks(False)
        QVBoxLayout(self).addWidget(self.browser)
        self.topics = load_topics()
        self.current: Topic | None = None

    def show_topic(self, help_id: str) -> Topic:
        topic = self.topics.get(help_id) or self.topics["index"]
        self.current = topic
        self.browser.setMarkdown(topic.file.read_text(encoding="utf-8"))
        found = self.browser.document().find(topic.heading)
        cursor = self.browser.textCursor()
        if not found.isNull():
            cursor.setPosition(found.position())
        else:
            cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.browser.setTextCursor(cursor)
        self.browser.ensureCursorVisible()
        self.setWindowTitle(f"Справка — {topic.heading}")
        self.show()
        self.raise_()
        return topic
