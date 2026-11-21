"""Значок программы — фирменный знак «Рувер»: icon.png (Linux) и icon.ico (Windows) в каталог."""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from qa_tool.gui.theme import draw_icon  # noqa: E402

if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    app = QGuiApplication([])
    image = draw_icon()
    image.save(str(out / "icon.png"))
    image.scaled(
        64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
    ).save(str(out / "icon.ico"))
