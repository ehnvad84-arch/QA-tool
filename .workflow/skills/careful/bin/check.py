#!/usr/bin/env python3
"""Хук PreToolUse для /careful: опасная команда — вопрос человеку, катастрофическая — запрет.

Читает JSON хука из stdin, смотрит tool_input.command (Bash) и отвечает в формате
Claude Code: hookSpecificOutput.permissionDecision = ask | deny. Безопасная команда —
пустой вывод и код 0. Идея и список шаблонов — gstack /careful (MIT), реализация своя.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

WRAPPERS = {"sudo", "doas", "env", "nice", "ionice", "nohup", "time", "xargs", "command", "exec", "busybox"}

# Каталоги сборки, которые можно удалять без вопроса
SAFE_RM = {"node_modules", ".next", "dist", "__pycache__", ".cache", "build", ".turbo", "coverage",
           ".pytest_cache", ".ruff_cache", ".mypy_cache", "target", ".venv"}

ASK = [
    (r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", "DROP — потеря данных"),
    (r"\bTRUNCATE\b", "TRUNCATE — потеря данных"),
    (r"\bDELETE\s+FROM\s+\w[\w.]*\s*(;|$|\")", "DELETE без WHERE — удаление всех строк"),
    (r"\bgit\s+push\b.*\s(--force(?!-with-lease)|-f)\b", "git push --force — переписывает историю"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard — потеря незакоммиченной работы"),
    (r"\bgit\s+(checkout|restore)\s+(--\s+)?\.(\s|$)", "откат всех изменений рабочего дерева"),
    (r"\bgit\s+clean\s+-\w*f", "git clean -f — удаление неотслеживаемых файлов"),
    (r"\bgit\s+branch\s+-D\b", "git branch -D — удаление ветки без проверки слияния"),
    (r"\bkubectl\s+delete\b", "kubectl delete — влияние на кластер"),
    (r"\bdocker\s+(rm\s+-f|system\s+prune|volume\s+(rm|prune)|image\s+prune)", "docker — потеря контейнеров, томов или образов"),
    (r"\bmkfs(\.\w+)?\b|\bdd\s+.*\bof=/dev/", "запись на устройство"),
    (r"\bchmod\s+-R\s+777\b", "chmod -R 777 — открывает всё всем"),
]


def _decision(kind: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": kind,
                                             "permissionDecisionReason": f"/careful: {reason}"}},
                     ensure_ascii=False))
    sys.exit(0)


def _default_branch(cwd: str) -> set[str]:
    names = {"main", "master"}
    try:
        ref = subprocess.run(["git", "-C", cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        if ref:
            names.add(ref.split("/", 1)[-1])
    except (OSError, subprocess.SubprocessError):
        pass
    return names


def _is_simple(cmd: str) -> bool:
    return not re.search(r"[;&|\n`]|\$\(", cmd)


def _rm_targets(cmd: str) -> list[str] | None:
    """Аргументы рекурсивного rm или None, если это не рекурсивное удаление."""
    for part in re.split(r"&&|\|\||;|\|", cmd):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        while words and (Path(words[0]).name in WRAPPERS or re.match(r"^\w+=", words[0])):
            words = words[1:]           # sudo, env, nice, xargs и присваивания VAR=… перед командой
        if not words or Path(words[0]).name != "rm":
            continue
        flags = [w for w in words[1:] if w.startswith("-")]
        recursive = any(w in ("-r", "-R", "--recursive") or (not w.startswith("--") and re.search(r"[rR]", w))
                        for w in flags)
        if recursive:
            return [w for w in words[1:] if not w.startswith("-")]
    return None


def check(payload: dict) -> tuple[str, str] | None:
    cmd = (payload.get("tool_input") or {}).get("command") or ""
    if not cmd.strip():
        return None
    cwd = payload.get("cwd") or "."
    targets = _rm_targets(cmd)
    if targets is not None:
        home_like = {"/", "~", "~/", "$HOME", "${HOME}", "/*", "~/*"}
        if _is_simple(cmd) and any(t in home_like for t in targets):
            return "deny", "рекурсивное удаление корня или домашнего каталога запрещено"
        if not targets or not all(Path(t.rstrip("/")).name in SAFE_RM for t in targets):
            return "ask", "рекурсивное удаление (rm -r) — подтвердите цель"
    push = re.search(r"\bgit\s+push\b(.*)", cmd)
    if push and re.search(r"\s(--force(?!-with-lease)|-f)\b", push.group(1)) and _is_simple(cmd):
        branches = _default_branch(cwd)
        if any(re.search(rf"(^|[\s:+]){re.escape(b)}(\s|$)", push.group(1)) for b in branches):
            return "deny", "force-push в основную ветку запрещён"
    for pattern, reason in ASK + _project_patterns(cwd):
        if re.search(pattern, cmd, re.IGNORECASE):
            return "ask", reason
    return None


def _project_patterns(cwd: str) -> list[tuple[str, str]]:
    """Свои шаблоны проекта: docs/rules/careful-patterns.txt, одно регулярное выражение на строку.
    Только добавляют предупреждения, отключить встроенные нельзя."""
    out: list[tuple[str, str]] = []
    try:
        root = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True,
                              text=True, timeout=3).stdout.strip() or cwd
        f = Path(root) / "docs" / "rules" / "careful-patterns.txt"
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    re.compile(line)
                    out.append((line, f"правило проекта: {line}"))
                except re.error:
                    continue
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)            # не наш формат — не мешаем работе
    res = check(payload)
    if res:
        _decision(*res)


if __name__ == "__main__":
    main()
