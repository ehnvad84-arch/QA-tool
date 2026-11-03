"""База прогона: `<папка прогонов>/<номер>/run.sqlite` и `evidence/`. Пишет только координатор."""

import datetime
import json
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path

from qa_tool.core.checks import CheckResult, CheckSpec, Evidence
from qa_tool.core.models import Scenario, Stand
from qa_tool.core.secrets import redact

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, started TEXT, finished TEXT, place TEXT, scenario TEXT, state TEXT
);
CREATE TABLE IF NOT EXISTS cameras (
    run TEXT, slot INTEGER, address TEXT, port INTEGER, vendor TEXT, username TEXT,
    manufacturer TEXT DEFAULT '', model TEXT DEFAULT '', firmware TEXT DEFAULT '',
    serial TEXT DEFAULT '', hardware TEXT DEFAULT '', PRIMARY KEY (run, slot)
);
CREATE TABLE IF NOT EXISTS results (
    run TEXT, slot INTEGER, check_id TEXT, module TEXT, title TEXT, description TEXT,
    phase INTEGER, severity TEXT, status TEXT, reason TEXT, expected TEXT, actual TEXT,
    threshold_value REAL, threshold_unit TEXT, threshold_source TEXT,
    evidence_dir TEXT, started TEXT, duration_s REAL
);
"""


def safe_name(name: str, limit: int = 80) -> str:
    """Имя файла из недоверенной строки: только буквы, цифры, точка, дефис, подчёркивание."""
    cleaned = re.sub(r"[^\w.-]", "_", name, flags=re.UNICODE).lstrip(".")
    cleaned = re.sub(r"\.{2,}", "_", cleaned)
    return (cleaned or "file")[:limit]


def next_run_id(runs_dir: Path, today: datetime.date | None = None) -> str:
    prefix = f"QA-{(today or datetime.date.today()):%Y%m%d}-"
    taken = [p.name for p in runs_dir.glob(prefix + "*")] if runs_dir.exists() else []
    numbers = [int(n[len(prefix) :]) for n in taken if n[len(prefix) :].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1:03d}"


class RunStore:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.run_id = run_dir.name
        self.evidence_root = run_dir / "evidence"
        self.db = sqlite3.connect(run_dir / "run.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    @classmethod
    def create(cls, stand: Stand, scenario: Scenario) -> "RunStore":
        stand.runs_dir.mkdir(parents=True, exist_ok=True)
        run_dir = stand.runs_dir / next_run_id(stand.runs_dir)
        run_dir.mkdir()
        store = cls(run_dir)
        store.db.execute(
            "INSERT INTO runs VALUES (?, ?, NULL, ?, ?, 'идёт')",
            (store.run_id, _now(), stand.place.value, json.dumps(asdict(scenario))),
        )
        for cam in stand.cameras:
            store.db.execute(
                "INSERT INTO cameras (run, slot, address, port, vendor, username) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (store.run_id, cam.slot, cam.address, cam.port, cam.vendor, cam.username),
            )
        store.db.commit()
        return store

    def protocol_id(self, slot: int) -> str:
        return f"{self.run_id}-{slot}"

    def set_passport(self, slot: int, passport: dict[str, str]) -> None:
        fields = ("manufacturer", "model", "firmware", "serial", "hardware")
        assignments = ", ".join(f"{f} = ?" for f in fields)
        self.db.execute(
            f"UPDATE cameras SET {assignments} WHERE run = ? AND slot = ?",
            (*(passport.get(f, "") for f in fields), self.run_id, slot),
        )
        self.db.commit()

    def _save_evidence(self, slot: int, check_id: str, evidence: list[Evidence]) -> str:
        if not evidence:
            return ""
        folder = self.evidence_root / str(slot) / safe_name(check_id)
        folder.mkdir(parents=True, exist_ok=True)
        for item in evidence:
            path = folder / safe_name(item.name)
            if isinstance(item.content, bytes):
                path.write_bytes(item.content)
            else:
                path.write_text(redact(item.content), encoding="utf-8")
        return folder.relative_to(self.run_dir).as_posix()

    def add_result(self, slot: int, spec: CheckSpec, result: CheckResult, started: str) -> None:
        t = result.threshold
        self.db.execute(
            "INSERT INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.run_id,
                slot,
                spec.id,
                spec.module,
                spec.title,
                spec.description,
                int(spec.phase),
                spec.severity.value,
                result.status.value,
                redact(result.reason),
                redact(result.expected),
                redact(result.actual),
                t.value if t else None,
                t.unit if t else None,
                t.source if t else None,
                self._save_evidence(slot, spec.id, result.evidence),
                started,
                result.duration_s,
            ),
        )
        self.db.commit()

    def finish(self, state: str = "завершён") -> None:
        self.db.execute(
            "UPDATE runs SET finished = ?, state = ? WHERE id = ?", (_now(), state, self.run_id)
        )
        self.db.commit()

    def run_info(self) -> sqlite3.Row:
        return self.db.execute("SELECT * FROM runs WHERE id = ?", (self.run_id,)).fetchone()

    def cameras(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM cameras WHERE run = ? ORDER BY slot", (self.run_id,)
        ).fetchall()

    def results(self, slot: int | None = None) -> list[sqlite3.Row]:
        query = "SELECT * FROM results WHERE run = ?"
        args: tuple = (self.run_id,)
        if slot is not None:
            query, args = query + " AND slot = ?", (*args, slot)
        return self.db.execute(query + " ORDER BY slot, phase, module, check_id", args).fetchall()

    def close(self) -> None:
        self.db.close()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")
