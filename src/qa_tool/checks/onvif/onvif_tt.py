"""ONVIF-TT: тесты onvif-tt (закреплённая версия) по камере через посредника табу.

Каждый тест onvif-tt — отдельная строка протокола со своим ID (`MEDIA2-1-1-4`), обменом SOAP
как доказательством и задачей программистам, если «не прошла». onvif-tt ходит к камере только
через `TabooProxy` (AD-7): запрещённое он отправить не может, а такой тест становится
«ошибкой стенда» с названием остановленной операции.

Изменяющие тесты onvif-tt идут только с галочкой «изменяющие»; перезагрузка — только при обеих
галочках, потому что `--allow-reboot` у onvif-tt сам включает изменяющие тесты.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from qa_tool.checks.onvif.excluded import EXCLUDED
from qa_tool.checks.onvif.ott_runner import PASSWORD_ENV
from qa_tool.core.checks import (
    REGISTRY,
    CheckContext,
    CheckResult,
    ChildResult,
    Evidence,
    Phase,
    Status,
)
from qa_tool.drivers.camera.taboo_proxy import TABOO_FAULT_CODE, TabooProxy

CHECK_ID = "ONVIF-TT"
MODULE_TIMEOUT_S = 1800.0  # 161 тест onvif-tt; на медленной камере — до ~15 мин, запас вдвое
STAND_FAULTS = (TABOO_FAULT_CODE, "QAToolForeignHost", "QAToolNoAnswer", "ServiceUnbindable")
STATUS = {
    "passed": Status.PASSED,
    "failed": Status.FAILED,
    "skipped": Status.SKIPPED,
    "error": Status.STAND_ERROR,
    "xfailed": Status.FAILED,
    "xpassed": Status.PASSED,
}
_SKIPPED = re.compile(r"Skipped: (.+?)['\")]*$", re.S)


def runner_command() -> list[str]:
    """Команда запуска `ott_runner`: модулем Python или из собранной программы (T14)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--ott-runner"]
    return [sys.executable, "-m", "qa_tool.checks.onvif.ott_runner"]


def titles() -> dict[str, str]:
    """Названия тестов onvif-tt — из docstring его реализаций («…html#tc.ID — название.»)."""
    from onvif_tt import registry

    registry.discover()
    out = {}
    for test_id, impl in registry.REGISTRY.items():
        first = (impl.func.__doc__ or "").strip().splitlines()[0] if impl.func.__doc__ else ""
        out[test_id] = first.split("—", 1)[-1].strip().rstrip(".") or test_id
    return out


def reason_from(longrepr: str, status: str) -> str:
    """Короткая причина для протокола из длинного вывода pytest."""
    if not longrepr:
        return ""
    if status == "skipped":
        match = _SKIPPED.search(longrepr)
        return f"onvif-tt пропустил: {match.group(1).strip()}" if match else "onvif-tt пропустил"
    lines = [line.strip() for line in str(longrepr).splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("E ")]
    return (errors[-1][2:].strip() if errors else lines[-1])[:500]


def to_child(item: dict, names: dict[str, str]) -> ChildResult:
    """Строка протокола из записи JSON-отчёта onvif-tt."""
    test_id, raw = item["id"], item.get("status", "error")
    longrepr = str(item.get("longrepr") or "")
    status = STATUS.get(raw, Status.STAND_ERROR)
    reason = reason_from(longrepr, raw)
    exchange = f"{item.get('last_request') or ''}\n\n{item.get('last_response') or ''}".strip()
    if any(code in longrepr or code in exchange for code in STAND_FAULTS):
        status = Status.STAND_ERROR
        stopped = TABOO_FAULT_CODE in longrepr or TABOO_FAULT_CODE in exchange
        reason = ("запрещённая операция не отправлена (табу): " if stopped else "") + reason
    elif raw == "xfailed":
        reason = f"известная ошибка прошивки (onvif-tt xfail): {reason}"
    elif raw == "xpassed":
        reason = "известная ошибка прошивки исправлена (onvif-tt xpass)"
    evidence = [Evidence(f"{test_id}.xml", exchange)] if exchange else []
    result = CheckResult(
        status,
        reason=reason,
        expected="поведение по ONVIF Test Specification",
        actual=reason if status is not Status.PASSED else "соответствует",
        evidence=evidence,
        duration_s=float(item.get("duration_s") or 0.0),
    )
    profiles = ", ".join(sorted(item.get("profiles") or [])) or "—"
    mandatory = "обязательный" if item.get("mandatory") else "необязательный"
    return ChildResult(
        test_id,
        names.get(test_id, test_id),
        result,
        description=f"Тест ONVIF Test Specification {test_id} (onvif-tt); профили: {profiles}, "
        f"{mandatory}.",
    )


def run_onvif_tt(ctx: CheckContext, proxy: TabooProxy, report: Path, timeout_s: float) -> str:
    """Прогнать onvif-tt против посредника; пароль — переменной окружения, не аргументом.

    Returns:
        Хвост вывода onvif-tt (для причины, если отчёта нет).

    Raises:
        subprocess.TimeoutExpired: прогон не уложился в `timeout_s`.
    """
    command = [
        *runner_command(),
        "--target",
        f"127.0.0.1:{proxy.port}",
        "--user",
        ctx.camera.username,
        "--report",
        str(report),
        "--exclude",
        *EXCLUDED,
    ]
    if ctx.allow_writes:
        command.append("--allow-writes")
    if ctx.allow_writes and ctx.allow_destructive:
        command.append("--allow-reboot")
    env = {**os.environ, PASSWORD_ENV: ctx.password}
    done = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout_s)
    return (done.stdout + done.stderr)[-2000:]


@REGISTRY.check(
    CHECK_ID,
    module="onvif",
    title="Тесты ONVIF (onvif-tt)",
    description="Тесты ONVIF Test Specification из onvif-tt через посредника табу: каждый тест — "
    "отдельная строка протокола.",
    phase=Phase.NETWORK,
    order=10,
    timeout_s=MODULE_TIMEOUT_S + 120,
)
def onvif_tt(ctx: CheckContext) -> CheckResult:
    """Прогнать onvif-tt по камере и вернуть строку протокола на каждый его тест."""
    proxy = TabooProxy(ctx.camera.address, ctx.camera.port).start()
    try:
        with tempfile.TemporaryDirectory(prefix="qa-tool-ott-") as folder:
            report = Path(folder) / "report.json"
            timeout_s = float(ctx.extra.get("onvif_tt_timeout_s", MODULE_TIMEOUT_S))
            try:
                tail = run_onvif_tt(ctx, proxy, report, timeout_s)
            except subprocess.TimeoutExpired:
                return CheckResult.stand_error(f"onvif-tt не уложился в {timeout_s:g} с")
            except OSError as e:
                return CheckResult.stand_error(f"onvif-tt не запустился: {e}")
            if not report.exists():
                return CheckResult.stand_error(f"onvif-tt не выдал отчёт: {tail[-300:]}")
            data = json.loads(report.read_text(encoding="utf-8"))
    except ValueError as e:
        return CheckResult.stand_error(f"отчёт onvif-tt не разобран: {e}")
    finally:
        proxy.stop()
    names = titles()
    children = [to_child(item, names) for item in data.get("results", [])]
    for test_id, why in EXCLUDED.items():
        children.append(
            ChildResult(
                test_id,
                names.get(test_id, test_id),
                CheckResult.skipped(f"исключён в QA-tool: {why}"),
            )
        )
    summary = data.get("summary", {})
    return CheckResult(
        Status.PASSED,
        actual=f"onvif-tt: {summary}",
        data={"onvif_tt_blocked": [b.reason for b in proxy.blocked]},
        children=children,
    )
