"""Пороги карты родного API (`api.*`): стартовые значения, уточняются на живых камерах.

Пределы защиты от враждебного ответа (размеры, число запросов) — не здесь, а константами кода
в `core/api_map.py` и `drivers/camera/native.py`: это не допуски проверки.
"""

from qa_tool.core.checks import CheckContext
from qa_tool.core.models import Threshold

START = "стартовое значение: уточнить на живых камерах"
DEFAULTS: dict[str, Threshold] = {
    "api.apply_delay_s": Threshold(2.0, "с", START),
    "api.baseline_gap_s": Threshold(2.0, "с", START),
    "api.share_low": Threshold(0.2, "доля", START),
    "api.share_high": Threshold(0.8, "доля", START),
    "api.scale_residual_pct": Threshold(2.0, "%", START),
    "api.compare_tol_pct": Threshold(1.0, "%", START),
    "api.range_tol_pct": Threshold(1.0, "%", START),
    "api.clamp_tol_pct": Threshold(2.0, "%", START),
    "api.request_timeout_s": Threshold(5.0, "с", START),
    "api.map_budget_s": Threshold(120.0, "с", START),
    "api.search_budget_s": Threshold(900.0, "с", START),
    "api.reconnect_wait_s": Threshold(120.0, "с", START),
}
RESERVE_S = 300  # возврат после автопоиска: идёт даже тогда, когда бюджет поиска исчерпан


def limit(ctx: CheckContext, key: str) -> Threshold:
    """Порог `api.*` из настроек стенда, иначе стартовый."""
    return ctx.thresholds.get(key, DEFAULTS[key])


def bad_thresholds(ctx: CheckContext) -> str:
    """Причина, по которой пороги не годятся; пусто — годятся.

    Ноль и отрицательное значение сделали бы проверку бессмысленной (допуск 0 — всё «не прошла»,
    бюджет 0 — ни одного запроса), а `share_low ≥ share_high` — две записи одного значения.
    """
    bad = [key for key in DEFAULTS if limit(ctx, key).value <= 0]
    if bad:
        return "значение не больше нуля: " + ", ".join(bad)
    if limit(ctx, "api.share_low").value >= limit(ctx, "api.share_high").value:
        return "api.share_low не меньше api.share_high"
    if limit(ctx, "api.share_high").value >= 1:
        return "api.share_high не меньше 1"
    return ""


def timeout_s() -> float:
    """Предел проверки для планировщика: карта, автопоиск, резерв возврата, ожидание камеры."""
    return (
        sum(
            DEFAULTS[key].value
            for key in ("api.map_budget_s", "api.search_budget_s", "api.reconnect_wait_s")
        )
        + RESERVE_S
    )
