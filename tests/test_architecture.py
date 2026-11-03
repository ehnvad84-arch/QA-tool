"""Табу держится в коде (AD-7): к камере ходят только через drivers/camera/http.py
(HTTP) и drivers/camera/probe.py, discovery.py (сокеты только на чтение)."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "qa_tool"
NETWORK = re.compile(
    r"^\s*(import|from)\s+(httpx|requests|urllib3|aiohttp|socket|http\.client|urllib\.request|zeep)\b",
    re.MULTILINE,
)
ALLOWED = {
    "drivers/camera/http.py": {"httpx"},
    "drivers/camera/probe.py": {"socket"},
    "drivers/camera/discovery.py": {"socket"},
}


def test_network_only_through_guarded_modules():
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        for match in NETWORK.finditer(path.read_text(encoding="utf-8")):
            if match.group(2) not in ALLOWED.get(rel, set()):
                offenders.append(f"{rel}: {match.group(0).strip()}")
    assert not offenders, "сеть в обход табу: " + "; ".join(offenders)
