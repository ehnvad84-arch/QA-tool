#!/usr/bin/env python3
"""/freeze: правки файлов разрешены только внутри одного каталога.

    freeze.py set <каталог>   — задать границу для текущего проекта
    freeze.py show            — показать границу
    freeze.py clear           — снять границу
    freeze.py hook            — хук PreToolUse (Edit, Write, NotebookEdit): вне границы — запрет

Граница хранится в ~/.claude/vw-state/, по файлу на проект (корень git или текущий каталог).
Идея — gstack /freeze (MIT), реализация своя.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

STATE_DIR = Path(os.environ.get("VW_STATE_DIR", Path.home() / ".claude" / "vw-state"))


def project_root(cwd: str) -> Path:
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=3).stdout.strip()
        if out:
            return Path(out).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).resolve()


def state_file(root: Path) -> Path:
    return STATE_DIR / f"freeze-{hashlib.sha1(str(root).encode()).hexdigest()[:16]}"


def boundary(root: Path) -> Path | None:
    f = state_file(root)
    try:
        return Path(f.read_text(encoding="utf-8").strip())
    except OSError:
        return None


def inside(path: Path, edge: Path) -> bool:
    try:
        path.relative_to(edge)
        return True
    except ValueError:
        return False


def hook() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)
    cwd = payload.get("cwd") or os.getcwd()
    edge = boundary(project_root(cwd))
    if edge is None:
        sys.exit(0)
    ti = payload.get("tool_input") or {}
    target = ti.get("file_path") or ti.get("notebook_path")
    if not target:
        sys.exit(0)
    p = Path(target)
    p = (Path(cwd) / p if not p.is_absolute() else p).resolve()
    if inside(p, edge):
        sys.exit(0)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"/freeze: правки разрешены только в {edge}; {p} вне границы. "
                                    "Снять границу — /unfreeze."}}, ensure_ascii=False))
    sys.exit(0)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "hook":
        hook()
        return 0
    root = project_root(os.getcwd())
    if cmd == "set":
        if len(argv) < 2:
            print("укажите каталог: freeze.py set <каталог>")
            return 2
        edge = Path(argv[1]).expanduser()
        edge = (root / edge if not edge.is_absolute() else edge).resolve()
        if not edge.is_dir():
            print(f"каталога нет: {edge}")
            return 1
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_file(root).write_text(str(edge), encoding="utf-8")
        print(f"граница правок: {edge}")
    elif cmd == "show":
        edge = boundary(root)
        print(f"граница правок: {edge}" if edge else "граница не задана")
    elif cmd == "clear":
        state_file(root).unlink(missing_ok=True)
        print("граница снята")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
