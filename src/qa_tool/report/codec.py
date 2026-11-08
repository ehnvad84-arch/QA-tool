"""Приложение протокола камеры «Кодеки: качество на битрейт» (спека codec-quality, UI).

Собирается из строк `CODEC-*` базы прогона и файлов их доказательств: `measure.json` (ступени,
битрейт в потоке, VMAF/SSIM/PSNR, эталон, пороги), `state.txt` и снимки. Строки одного
кодировщика (`CODEC-H264-n`, `CODEC-H265-n`) сводятся в один лист с графиком «VMAF — битрейт».
График рисуется при сборке протокола в папку доказательств кодировщика; пути к файлам строит
программа из ID строки, а не из ответа камеры. Числа и тексты из `measure.json` и `state.txt` —
от камеры и недоверенные: шаблон их экранирует, числа проверяются на конечность.
"""

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from qa_tool.core.checks import Status
from qa_tool.report.charts import XYSeries, xy_chart

PREFIX = "CODEC-"
STATE_ID, RESTORE_ID, LOST_ID = "CODEC-STATE-1", "CODEC-RESTORE-1", "CODEC-WRITE-LOST"
MEASURE_LIMIT = 1 << 20  # measure.json кодировщика — килобайты; больше — не наш файл
CODEC_TITLES = {"H264": "H.264", "H265": "H.265"}
PLACES = {"H264": 0, "H265": 1}  # H.264 — круг, сплошная; H.265 — квадрат, штрих (макет UI)
TICKS_KBPS = [64 * 2**i for i in range(11)]  # 0,0625 … 64 Мбит/с — деления оси битрейта


@dataclass
class StepLine:
    """Строка таблицы ступеней листа кодировщика."""

    codec: str
    step: str  # «2 из 5»
    target: str  # «4 Мбит/с»
    measured: str  # «4,1 (+2 %)»
    vmaf: str
    ssim: str
    psnr: str
    status: str
    bad_rate: bool = False
    bad_vmaf: bool = False


@dataclass
class CodecLine:
    """Строка листа «Кодеки» Excel — одна ступень; числа числами, чтобы ОТК строил графики."""

    check_id: str
    profile: int
    names: str
    size: str
    fps: float | None
    codec: str
    step: str
    target_kbps: float | None
    measured_kbps: float | None
    off_pct: float | None
    vmaf: float | None
    ssim: float | None
    psnr: float | None
    reference: str
    vmaf_min: float | None
    source: str
    status: str
    reason: str
    links: list[str] = field(default_factory=list)  # снимок ступени и эталона


@dataclass
class CodecVerdict:
    """Итог кодека на листе: словом и с причиной."""

    codec: str
    check_id: str
    status: str
    reason: str


@dataclass
class EncoderSheet:
    """Лист одного кодировщика: заголовок, график, итоги кодеков, ступени, снимки."""

    number: int
    heading: str
    chart: str  # путь к PNG относительно папки прогона; пусто — точек нет
    chart_note: str
    verdicts: list[CodecVerdict]
    better: str
    steps: list[StepLine]
    shots: list[tuple[str, str, str]]  # (миниатюра, полный кадр, подпись)


@dataclass
class CodecAppendix:
    """Приложение «Кодеки» к протоколу одной камеры."""

    number: int
    allowed: bool
    restore_status: str
    restore_reason: str
    state_status: str
    state_lines: list[str]
    state_images: list[tuple[str, str]]  # (кадр, подпись)
    sheets: list[EncoderSheet]
    without: list[tuple[str, str, str]]  # (строка, итог, причина) — строки без замера
    lost: list[tuple[str, str]] = field(default_factory=list)  # журнал прошлого прогона
    lines: list[CodecLine] = field(default_factory=list)  # строки листа «Кодеки» Excel


def _finite(value) -> float | None:
    return float(value) if isinstance(value, int | float) and math.isfinite(value) else None


def _num(value, digits: int = 1) -> str:
    found = _finite(value)
    return "—" if found is None else f"{found:.{digits}f}".replace(".", ",")


def _mbit(kbps) -> str:
    """Битрейт в Мбит/с для таблиц и делений: «0,5», «2», «0,06»."""
    found = _finite(kbps)
    if found is None:
        return "—"
    value = found / 1024
    text = f"{value:.2f}".rstrip("0").rstrip(".") if value < 1 else f"{value:.3g}"
    return text.replace(".", ",")


def _read(path: Path, limit: int = MEASURE_LIMIT) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _measure(folder: Path) -> dict:
    try:
        data = json.loads(_read(folder / "measure.json") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) and data.get("версия") == 1 else {}


def _quality(step: dict, name: str) -> float | None:
    found = (step.get("качество") or {}).get(name)
    return _finite(found[0]) if isinstance(found, list) and found else None


def _rule(measure: dict, suffix: str) -> float | None:
    rule = measure.get("правило") or {}
    found = next((v for k, v in rule.items() if str(k).endswith(suffix)), None)
    return _finite(found.get("значение")) if isinstance(found, dict) else None


def _step_lines(codec: str, measure: dict) -> list[StepLine]:
    """Ступени кодека от большего битрейта к меньшему, нарушения помечены (не только цветом)."""
    steps = [s for s in measure.get("ступени", []) if isinstance(s, dict)]
    steps.sort(key=lambda s: -(_finite(s.get("задано")) or 0))
    vmaf_min, floor = _rule(measure, ".vmaf_min"), _rule(measure, ".kbps_floor")
    pct = _rule(measure, "bitrate_pct")
    lines = []
    for index, step in enumerate(steps, start=1):
        target, got = _finite(step.get("задано")), _finite(step.get("в потоке"))
        off = (got / target - 1) * 100 if target and got is not None else None
        bad_rate = off is not None and pct is not None and abs(off) > pct
        vmaf = _quality(step, "vmaf")
        bad_vmaf = (
            vmaf is not None
            and vmaf_min is not None
            and (floor is None or (target or 0) >= floor)
            and vmaf < vmaf_min
        )
        problem = step.get("не принято") or step.get("поток не вернулся")
        failed = bool(problem) or bad_rate or bad_vmaf
        status = Status.FAILED.value if failed else Status.PASSED.value
        if problem:
            status = f"{status}: {problem}"
        elif step.get("качество") is None:
            status = "нет замера"
        measured = "—" if off is None else f"{_mbit(got)} ({off:+.0f} %)".replace("-", "−")
        lines.append(
            StepLine(
                CODEC_TITLES.get(codec, codec),
                f"{index} из {len(steps)}",
                f"{_mbit(target)} Мбит/с",
                measured,
                _num(vmaf),
                _num(_quality(step, "ssim"), 3),
                _num(_quality(step, "psnr")),
                status,
                bad_rate,
                bad_vmaf,
            )
        )
    return lines


def _points(measure: dict) -> list[tuple[float, float]]:
    """Точки графика: X — битрейт в потоке (нет — заданный), Y — VMAF."""
    points = []
    for step in measure.get("ступени", []):
        if not isinstance(step, dict):
            continue
        x = _finite(step.get("в потоке")) or _finite(step.get("задано"))
        y = _quality(step, "vmaf")
        if x and y is not None:
            points.append((x, y))
    return points


def _ticks(points: list[tuple[float, float]]) -> list[tuple[float, str]]:
    """Деления оси битрейта — степени двойки вокруг точек, не меньше трёх."""
    low = min(x for x, _y in points)
    high = max(x for x, _y in points)
    chosen = [t for t in TICKS_KBPS if low / 2 < t < high * 2]
    while len(chosen) < 3:
        smaller = [t for t in TICKS_KBPS if t < chosen[0]] if chosen else []
        chosen = ([smaller[-1]] if smaller else []) + chosen + [chosen[-1] * 2 if chosen else 1024]
    return [(t, _mbit(t)) for t in chosen]


def _better(measures: dict[str, dict], spread: float) -> str:
    """Строка S2: какой кодек лучше при равном битрейте и насколько."""
    if len(measures) < 2:
        return ""
    by_codec = {
        codec: {
            _finite(s.get("задано")): _quality(s, "vmaf")
            for s in m.get("ступени", [])
            if isinstance(s, dict)
        }
        for codec, m in measures.items()
    }
    (a, va), (b, vb) = list(by_codec.items())[:2]
    common = [k for k in va if k in vb and va[k] is not None and vb[k] is not None]
    if not common:
        return "При равном битрейте кодеки не сравнить: общих ступеней нет."
    diffs = [vb[k] - va[k] for k in common]
    mean = sum(diffs) / len(diffs)
    if abs(mean) <= spread:
        return f"При равном битрейте кодеки равны в пределах разброса {_num(spread)} VMAF."
    winner = b if mean > 0 else a
    wins = sum(1 for d in diffs if (d > 0) == (mean > 0) and abs(d) > 0)
    where = "на всех ступенях" if wins == len(diffs) else f"на {wins} из {len(diffs)} ступеней"
    return (
        f"При равном битрейте лучше {CODEC_TITLES.get(winner, winner)}: {where}, в среднем на "
        f"{_num(abs(mean))} VMAF."
    )


def _sheet(number: int, rows: list, run_dir: Path) -> EncoderSheet:
    measures: dict[str, dict] = {}
    folders: dict[str, str] = {}
    verdicts = []
    for row in sorted(rows, key=lambda r: r.check_id):
        codec = row.check_id.split("-")[1]
        verdicts.append(
            CodecVerdict(CODEC_TITLES.get(codec, codec), row.check_id, row.status, row.reason)
        )
        if row.evidence:
            found = _measure(run_dir / row.evidence)
            if found:
                measures[codec] = found
                folders[codec] = row.evidence
    first = next(iter(measures.values()), {})
    size = first.get("разрешение") or [None, None]
    width, height = (size + [None, None])[:2] if isinstance(size, list) else (None, None)
    profiles = (first.get("кодировщик") or {}).get("профили") or []
    names = ", ".join(
        str(p[1] or p[0]) for p in profiles if isinstance(p, list) and len(p) == 2
    ) or next((r.title.split(" — ", 1)[-1] for r in rows), "")
    reference = first.get("эталон") or {}
    ref_codec = str(reference.get("кодек", ""))
    heading = f"Профиль {number} · {names}"
    if width and height:
        heading += f" · {width}×{height}"
    if _finite(first.get("частота")):
        heading += f" · {_num(first.get('частота'), 0)} к/с"
    if ref_codec:
        heading += (
            f" · эталон {CODEC_TITLES.get(ref_codec, ref_codec)}, "
            f"{_mbit(reference.get('битрейт'))} Мбит/с"
        )
    steps = [line for codec, m in measures.items() for line in _step_lines(codec, m)]
    chart, note = _chart(number, measures, folders, run_dir, width, height)
    spread = _rule(first, "vmaf_spread") or 2.0
    return EncoderSheet(
        number,
        heading,
        chart,
        note,
        verdicts,
        _better(measures, spread),
        steps,
        _shots(measures, folders, ref_codec, run_dir),
    )


def _chart(
    number: int,
    measures: dict[str, dict],
    folders: dict[str, str],
    run_dir: Path,
    width,
    height,
) -> tuple[str, str]:
    """График кодировщика в папку его доказательств; без точек — пусто и причина."""
    series = [
        XYSeries(CODEC_TITLES.get(codec, codec), _points(m), PLACES.get(codec, 2))
        for codec, m in measures.items()
        if _points(m)
    ]
    if not series:
        return "", "Точек для графика нет: VMAF не посчитан ни на одной ступени."
    first = next(iter(measures.values()))
    level = _rule(first, ".vmaf_min")
    size = f"{width}×{height}" if width and height else ""
    threshold = (level, f"порог {size}: {_num(level, 0)}") if level is not None else None
    target = Path(next(iter(folders.values()))).parent / f"codec-curve-{number}.png"
    (run_dir / target).parent.mkdir(parents=True, exist_ok=True)
    xy_chart(
        run_dir / target,
        f"Профиль {number}: качество к эталону",
        "Битрейт, Мбит/с (логарифмическая шкала)",
        "VMAF",
        series,
        _ticks([p for s in series for p in s.points]),
        threshold=threshold,
    )
    missing = [c for c in measures if not _points(measures[c])]
    note = ""
    if missing:
        titles = ", ".join(CODEC_TITLES.get(c, c) for c in missing)
        note = f"Нет точек: {titles} — VMAF не посчитан."
    return target.as_posix(), note


def _shots(
    measures: dict[str, dict], folders: dict[str, str], ref_codec: str, run_dir: Path
) -> list[tuple[str, str, str]]:
    """Эталон и худшая ступень — целиком: на них видно, что теряется при сжатии."""
    shots = []
    if ref_codec in folders and (run_dir / folders[ref_codec] / "ref_t.jpg").exists():
        folder = folders[ref_codec]
        caption = f"{CODEC_TITLES.get(ref_codec, ref_codec)} · эталон"
        shots.append((f"{folder}/ref_t.jpg", f"{folder}/ref.jpg", caption))
    worst: tuple[float, str, str, str] | None = None
    for codec, measure in measures.items():
        steps = sorted(
            (s for s in measure.get("ступени", []) if isinstance(s, dict)),
            key=lambda s: -(_finite(s.get("задано")) or 0),
        )
        for index, step in enumerate(steps, start=1):
            vmaf = _quality(step, "vmaf")
            if vmaf is None or (worst is not None and vmaf >= worst[0]):
                continue
            name = f"s{index}"
            if (run_dir / folders[codec] / f"{name}_t.jpg").exists():
                caption = (
                    f"{CODEC_TITLES.get(codec, codec)} · {_mbit(step.get('задано'))} Мбит/с · "
                    f"VMAF {_num(vmaf)}"
                )
                worst = (vmaf, folders[codec], name, caption)
    if worst is not None:
        _vmaf, folder, name, caption = worst
        shots.append((f"{folder}/{name}_t.jpg", f"{folder}/{name}.jpg", caption))
    return shots


def build_codec_appendix(
    rows: list, run_dir: Path, allowed: bool, number: int = 1
) -> CodecAppendix | None:
    """Приложение по строкам протокола; None — строк `CODEC-*` нет.

    Args:
        rows: строки протокола (`report.model.Row`).
        run_dir: папка прогона — от неё отсчитаны пути доказательств; сюда же ляжет график.
        allowed: стояла ли галочка «изменяющие».
        number: номер приложения в протоколе.
    """
    codec = [r for r in rows if r.check_id.startswith(PREFIX)]
    if not codec:
        return None
    state = next((r for r in codec if r.check_id == STATE_ID), None)
    restore = next((r for r in codec if r.check_id == RESTORE_ID), None)
    state_lines = (
        _read(run_dir / state.evidence / "state.txt").splitlines()
        if state and state.evidence
        else []
    )
    state_images = []
    if state and state.evidence:
        for path in sorted((run_dir / state.evidence).glob("state-*.jpg")):
            if path.name.endswith("_t.jpg"):
                continue
            label = path.stem.removeprefix("state-")
            state_images.append((f"{state.evidence}/{path.name}", f"профиль {label}"))
    groups: dict[int, list] = {}
    without = []
    lost = []
    for row in codec:
        if row.check_id in (STATE_ID, RESTORE_ID):
            continue
        if row.check_id == LOST_ID:
            lost.append((row.status, row.reason))
            continue
        number_text = row.check_id.rsplit("-", 1)[-1]
        if not number_text.isdigit():
            continue
        measured = row.evidence and (run_dir / row.evidence / "measure.json").exists()
        if not measured:
            without.append((row.title, row.status, row.reason))
            continue
        groups.setdefault(int(number_text), []).append(row)
    sheets = [_sheet(n, groups[n], run_dir) for n in sorted(groups)]
    lines = [line for n in sorted(groups) for row in groups[n] for line in _lines(n, row, run_dir)]
    return CodecAppendix(
        number=number,
        allowed=allowed,
        restore_status=restore.status if restore else "",
        restore_reason=restore.reason if restore else "",
        state_status=state.status if state else "",
        state_lines=state_lines,
        state_images=state_images,
        sheets=sheets,
        without=without,
        lost=lost,
        lines=lines,
    )


def _lines(number: int, row, run_dir: Path) -> list[CodecLine]:
    """Ступени строки протокола для Excel, от большего битрейта к меньшему."""
    measure = _measure(run_dir / row.evidence)
    codec = str(measure.get("кодек", row.check_id.split("-")[1]))
    size = measure.get("разрешение")
    size_text = f"{size[0]}×{size[1]}" if isinstance(size, list) and len(size) == 2 else ""
    profiles = (measure.get("кодировщик") or {}).get("профили") or []
    names = ", ".join(str(p[1] or p[0]) for p in profiles if isinstance(p, list) and len(p) == 2)
    reference = measure.get("эталон") or {}
    ref_codec = str(reference.get("кодек", ""))
    ref_text = (
        f"{CODEC_TITLES.get(ref_codec, ref_codec)}, {_mbit(reference.get('битрейт'))} Мбит/с"
        if ref_codec
        else ""
    )
    rule = measure.get("правило") or {}
    vmaf_rule = next((v for k, v in rule.items() if str(k).endswith(".vmaf_min")), {})
    steps = sorted(
        (s for s in measure.get("ступени", []) if isinstance(s, dict)),
        key=lambda s: -(_finite(s.get("задано")) or 0),
    )
    shown = {line.step: line for line in _step_lines(codec, measure)}
    out = []
    for index, step in enumerate(steps, start=1):
        target, got = _finite(step.get("задано")), _finite(step.get("в потоке"))
        place = f"{index} из {len(steps)}"
        links = [
            f"{row.evidence}/{name}.jpg"
            for name in (f"s{index}", "ref")
            if (run_dir / row.evidence / f"{name}.jpg").exists()
        ]
        line = shown.get(place)
        out.append(
            CodecLine(
                check_id=row.check_id,
                profile=number,
                names=names,
                size=size_text,
                fps=_finite(measure.get("частота")),
                codec=CODEC_TITLES.get(codec, codec),
                step=place,
                target_kbps=target,
                measured_kbps=got,
                off_pct=round((got / target - 1) * 100, 1) if target and got is not None else None,
                vmaf=_quality(step, "vmaf"),
                ssim=_quality(step, "ssim"),
                psnr=_quality(step, "psnr"),
                reference=ref_text,
                vmaf_min=_finite(vmaf_rule.get("значение"))
                if isinstance(vmaf_rule, dict)
                else None,
                source=str(vmaf_rule.get("источник", "")) if isinstance(vmaf_rule, dict) else "",
                status=line.status.split(":")[0] if line else "",
                reason=row.reason,
                links=links,
            )
        )
    return out
