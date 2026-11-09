"""Проверка собранной программы: веб-обход виртуальной камеры без скачиваний (S23).

Запуск из корня репозитория:
    uv run python packaging/smoke_web.py <программа> [аргументы до «run» …]

Ломается именно в сборке: браузер должен лежать в ней самой, поэтому переменные, которые указывают
на браузер разработчика, из окружения убираются, а папка браузера у Playwright подменяется пустой.
Код выхода: 0 — сборка годна, 1 — нет.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.mock_camera import MockCamera, Passport, stand_yaml  # noqa: E402

from qa_tool.core.store import RunStore  # noqa: E402

PAGE = "<h1>Камера</h1><button onclick=\"fetch('/seen')\">Обновить</button>"


def main(app: list[str]) -> int:
    """Прогнать WEB-WALK-1 собранной программой и проверить строки протокола."""
    if Path(app[0]).exists():
        app = [str(Path(app[0]).resolve()), *app[1:]]
    camera = MockCamera(Passport("Smoke", "M1", "1.0", "SN1"), web_pages={"/": PAGE}).start()
    try:
        with tempfile.TemporaryDirectory(prefix="qa-tool-smoke-web-") as tmp:
            folder = Path(tmp)
            (folder / "stand.yaml").write_text(stand_yaml([camera]), encoding="utf-8")
            (folder / "scenario.yaml").write_text("checks: [WEB-WALK-1]\n", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if not k.startswith("PLAYWRIGHT")}
            env |= {"QA_TOOL_PASSWORD_1": "admin", "HOME": str(folder), "USERPROFILE": str(folder)}
            done = subprocess.run(
                [*app, "run", "--stand", str(folder / "stand.yaml")],
                cwd=folder,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            print(done.stdout[-3000:], done.stderr[-3000:], sep="\n")
            runs = sorted(p for p in (folder / "runs").glob("QA-*") if p.is_dir())
            if not runs:
                print("ПРОВАЛ: прогон не создал папку результатов")
                return 1
            store = RunStore(runs[-1], readonly=True)
            rows = {r["check_id"]: (r["status"], r["reason"]) for r in store.results()}
            store.close()
    finally:
        camera.stop()
    pages = [i for i in rows if i.startswith("WEB-PAGE-")]
    print(f"строк страниц: {len(pages)}; сводка: {rows.get('WEB-SUMMARY-1')}")
    if rows.get("WEB-SUMMARY-1", ("",))[0] != "прошла" or not pages:
        print("ПРОВАЛ: обход не состоялся:", rows.get("WEB-WALK-1"))
        return 1
    return 0


if __name__ == "__main__":
    # консоль Windows в CI — cp1252: русский текст протокола иначе роняет печать
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:] or ["qa-tool"]))
