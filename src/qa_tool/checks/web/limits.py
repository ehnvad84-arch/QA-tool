"""Пороги веб-обхода: стартовые значения, уточняются на настоящих камерах."""

from qa_tool.core.checks import CheckContext
from qa_tool.core.models import Threshold

START = "стартовое значение"
DEFAULTS: dict[str, Threshold] = {
    "web.page_load_s": Threshold(5.0, "с", START),
    "web.click_response_s": Threshold(2.0, "с", START),
    "web.login_timeout_s": Threshold(15.0, "с", START),
    "web.same_elements_limit": Threshold(3.0, "шт", START),
    "web.pages_limit": Threshold(100.0, "шт", START),
    "web.elements_limit": Threshold(1000.0, "шт", START),
    "web.walk_limit_min": Threshold(30.0, "мин", START),
    "web.writes_limit": Threshold(20.0, "шт", START),
    "web.restore_timeout_s": Threshold(20.0, "с", START),
    "web.camera_return_s": Threshold(180.0, "с", START),
    # запись на «Рувер» только с отдельного разрешения директора: галочка общая на сценарий
    "web.ruver_writes": Threshold(0.0, "", START),
}


def limit(ctx: CheckContext, key: str) -> float:
    """Значение порога из настроек стенда, иначе стартовое."""
    return ctx.thresholds.get(key, DEFAULTS[key]).value
