"""Подсказки и справка: у каждого элемента — tooltip «что это и что делать» и help_id раздела F1."""

from PySide6.QtWidgets import QProxyStyle, QStyle, QWidget

TOOLTIP_DELAY_MS = 1000
HELP_ID = "help_id"


class ToolTipStyle(QProxyStyle):
    """Подсказка появляется, когда мышь держится на элементе 1 секунду."""

    def styleHint(self, hint, option=None, widget=None, returnData=None):  # noqa: N802, N803
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return TOOLTIP_DELAY_MS
        return super().styleHint(hint, option, widget, returnData)


def helped(widget: QWidget, tooltip: str, help_id: str) -> QWidget:
    widget.setToolTip(tooltip)
    widget.setProperty(HELP_ID, help_id)
    return widget


def help_id_of(widget: QWidget | None) -> str:
    """help_id элемента или ближайшего родителя (экрана)."""
    while widget is not None:
        value = widget.property(HELP_ID)
        if value:
            return str(value)
        widget = widget.parentWidget()
    return "index"
