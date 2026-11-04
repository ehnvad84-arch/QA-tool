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
from qa_tool.checks.onvif.snapshot import Snapshot, SnapshotTaker
from qa_tool.core.checks import (
    REGISTRY,
    CheckContext,
    CheckResult,
    ChildResult,
    Evidence,
    Phase,
    Status,
)
from qa_tool.drivers.camera.soap import CameraUnreachable, OnvifFault, SoapClient
from qa_tool.drivers.camera.taboo_proxy import TABOO_FAULT_CODE, TabooProxy

CHECK_ID = "ONVIF-TT"
SNAPSHOT_ID = "RUV-SNAPSHOT-1"
RESTORE_ID = "RUV-RESTORE-1"
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


RUNNER_FLAG = "--ott-runner"  # packaging/entry.py: собранная программа становится запускателем


def runner_command() -> list[str]:
    """Команда запуска `ott_runner`: модулем Python или из собранной программы.

    В сборке Windows оконный qa-tool.exe не имеет консоли — у pytest не было бы stdout,
    поэтому запускателем служит консольный qa-tool-cli.exe из той же папки (окно консоли
    не показывается: CREATE_NO_WINDOW в `_run`).
    """
    if not getattr(sys, "frozen", False):
        return [sys.executable, "-m", "qa_tool.checks.onvif.ott_runner"]
    console = Path(sys.executable).with_name("qa-tool-cli.exe")
    return [str(console if console.exists() else sys.executable), RUNNER_FLAG]


def path_without_ffprobe(path: str) -> str:
    """PATH для onvif-tt без папок с ffprobe: вторая защита к исключению его тестов.

    Тест onvif-tt, не попавший в список исключений (новая версия), без ffprobe пропускается
    сам, а не передаёт пароль камеры в командной строке (см. excluded.py).
    """
    exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    return os.pathsep.join(
        folder for folder in path.split(os.pathsep) if folder and not (Path(folder) / exe).exists()
    )


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
        if "--allow-reboot" in longrepr:
            return "не разрешено сценарием: перезагрузка камеры (нужны обе галочки)"
        if "--allow-writes" in longrepr:
            return "не разрешено сценарием: изменяющая проверка"
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


def run_onvif_tt(
    ctx: CheckContext, proxy: TabooProxy, report: Path, timeout_s: float, writes: bool
) -> str:
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
    if writes:
        command.append("--allow-writes")
    if writes and ctx.allow_destructive:
        command.append("--allow-reboot")
    # вывод pytest читается как UTF-8 и на Windows, где консоль по умолчанию в cp866/cp1251
    env = {
        **os.environ,
        PASSWORD_ENV: ctx.password,
        "PYTHONIOENCODING": "utf-8",
        "PATH": path_without_ffprobe(os.environ.get("PATH", "")),
    }
    done = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # 0 — не Windows
    )
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
    group="Тесты onvif-tt",
    help_id="onvif-tt",
)
def onvif_tt(ctx: CheckContext) -> CheckResult:
    """Прогнать onvif-tt по камере и вернуть строку протокола на каждый его тест.

    С галочкой «изменяющие» перед прогоном сохраняются настройки, которые меняют изменяющие
    тесты, а после — возвращаются и сверяются (строка `RUV-RESTORE-1`), даже если onvif-tt
    упал или завис. Не удалось сохранить — изменяющие тесты не запускаются.
    """
    extra_rows: list[ChildResult] = []
    taker, snap, writes = None, None, False
    if ctx.allow_writes:
        taker = SnapshotTaker(ctx.camera, SoapClient(ctx.camera.username, ctx.password))
        try:
            snap = taker.take()
        except (CameraUnreachable, OnvifFault, ValueError) as e:
            snap = Snapshot(skipped=[str(e)])
        writes = not snap.skipped
        if snap.skipped:
            extra_rows.append(
                ChildResult(
                    SNAPSHOT_ID,
                    "Сохранение настроек перед изменяющими тестами",
                    CheckResult.stand_error(
                        "настройки не сохранены — изменяющие тесты onvif-tt "
                        "не запускались: " + "; ".join(snap.skipped)
                    ),
                )
            )
    proxy = TabooProxy(ctx.camera.address, ctx.camera.port).start()
    restore_row = None
    try:
        result = _run(ctx, proxy, writes)
    except Exception as e:  # прогон сорвался — настройки всё равно возвращаем (finally)
        result = CheckResult.stand_error(f"сбой прогона onvif-tt: {type(e).__name__}: {e}")
    finally:
        proxy.stop()
        if writes and taker and snap:
            restore_row = _restore_row(taker, snap)
    if not extra_rows and restore_row is None:
        return result
    # прогон без строк (сорвался) — его итог отдельной строкой, чтобы рядом была строка
    # о восстановлении настроек
    rows = result.children or [ChildResult(CHECK_ID, "Прогон onvif-tt", result)]
    return CheckResult(
        result.status,
        actual=result.actual,
        data=result.data,
        children=[*extra_rows, *rows, *([restore_row] if restore_row else [])],
    )


def _restore_row(taker: "SnapshotTaker", snap: "Snapshot") -> ChildResult:
    """Строка протокола «вернули ли настройки камеры как были» (AD-3, never.md)."""
    restored, problems = taker.restore(snap)
    evidence = [Evidence("restored.txt", "Восстановлено и сверено:\n" + "\n".join(restored))]
    if problems:
        result = CheckResult(
            Status.STAND_ERROR,
            reason="камера осталась изменённой: " + "; ".join(problems),
            expected="настройки после изменяющих тестов совпадают с исходными",
            actual="; ".join(problems),
            evidence=evidence,
        )
    else:
        result = CheckResult(
            Status.PASSED,
            expected="настройки после изменяющих тестов совпадают с исходными",
            actual=f"восстановлено и сверено: {', '.join(restored) or 'менять было нечего'}",
            evidence=evidence,
        )
    return ChildResult(RESTORE_ID, "Восстановление настроек после изменяющих тестов", result)


def _run(ctx: CheckContext, proxy: TabooProxy, writes: bool) -> CheckResult:
    """Прогнать onvif-tt и разобрать его отчёт в строки протокола."""
    try:
        with tempfile.TemporaryDirectory(prefix="qa-tool-ott-") as folder:
            report = Path(folder) / "report.json"
            # выше предела планировщик снимет процесс раньше, и настройки не восстановятся
            timeout_s = min(
                float(ctx.extra.get("onvif_tt_timeout_s", MODULE_TIMEOUT_S)), MODULE_TIMEOUT_S
            )
            try:
                tail = run_onvif_tt(ctx, proxy, report, timeout_s, writes)
            except subprocess.TimeoutExpired:
                return CheckResult.stand_error(f"onvif-tt не уложился в {timeout_s:g} с")
            except OSError as e:
                return CheckResult.stand_error(f"onvif-tt не запустился: {e}")
            if not report.exists():
                return CheckResult.stand_error(f"onvif-tt не выдал отчёт: {tail[-300:]}")
            data = json.loads(report.read_text(encoding="utf-8"))
    except ValueError as e:
        return CheckResult.stand_error(f"отчёт onvif-tt не разобран: {e}")
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
    return CheckResult(
        Status.PASSED,
        actual=f"onvif-tt: {data.get('summary', {})}",
        data={"onvif_tt_blocked": [b.reason for b in proxy.blocked]},
        children=children,
    )
