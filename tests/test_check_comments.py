import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "check_comments", Path(__file__).resolve().parents[1] / "scripts" / "check_comments.py"
)
check_comments = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_comments)


def test_project_follows_comment_rules():
    assert check_comments.main([]) == 0


def test_english_and_empty_todo_caught(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        '"""Parse the camera answer."""\n# TODO: fix\n# retry because firmware drops link\nx = 1\n',
        encoding="utf-8",
    )
    problems = check_comments.check_file(bad)
    assert len(problems) == 3


def test_russian_and_tool_directives_pass(tmp_path):
    good = tmp_path / "good.py"
    good.write_text(
        '"""Разбор ответа камеры."""\nimport os  # noqa: F401\n'
        "# Камеры Dahua шлют `XAddrs` без порта — берём 80.\n"
        "# TODO: повторить запрос после переподключения; задача QA-12\nx = 1\n",
        encoding="utf-8",
    )
    assert check_comments.check_file(good) == []
