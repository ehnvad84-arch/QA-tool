"""Отказ камеры в пароле для видео: одна попытка на камеру за весь прогон.

Неверный пароль на камере уменьшает счётчик попыток (блокировка), поэтому после отказа ни одна
проверка с RTSP и ни одна нить с этим паролем больше не идёт. ONVIF при этом может пройти: у камеры
бывают разные учётные записи для ONVIF и RTSP. Память — в `ctx.extra`: она живёт весь прогон камеры
и общая для проверок и нитей.
"""

from qa_tool.core.checks import CheckContext, CheckResult
from qa_tool.drivers.camera.rtsp import PASSWORD_REFUSED_CODES, RtspError

MEMORY_KEY = "rtsp_password_refused"


def remember_refusal(ctx: CheckContext, error: Exception) -> str:
    """Запомнить отказ и вернуть причину для строки протокола."""
    reason = f"камера не приняла логин и пароль для видео: {error}"
    ctx.extra[MEMORY_KEY] = reason
    return reason


def refused_password(ctx: CheckContext) -> str:
    """Причина, если камера уже отвергла пароль на этом прогоне; пусто — не отвергала."""
    return ctx.extra.get(MEMORY_KEY, "")


def is_refusal(error: Exception) -> bool:
    """Ошибка RTSP значит отказ в пароле: 403 — так камера отвечает при блокировке входа."""
    return isinstance(error, RtspError) and error.status in PASSWORD_REFUSED_CODES


def refusal_result(ctx: CheckContext, error: Exception) -> CheckResult | None:
    """«Ошибка стенда», если `error` — отказ в пароле; иначе None."""
    return CheckResult.stand_error(remember_refusal(ctx, error)) if is_refusal(error) else None
