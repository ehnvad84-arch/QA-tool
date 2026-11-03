"""Проверка правил комментирования (docs/rules/comments.md) — то, что проверяется машинно.

Ловит комментарии и docstring не на русском и TODO/FIXME без внятного описания.
Закомментированный код и отсутствие docstring у публичного API ловит ruff (ERA001, D1).
Смысл комментария («почему, а не что», актуальность) проверяется на ревью, не здесь.

    python scripts/check_comments.py [пути]   # по умолчанию: src tests scripts packaging
"""

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

DEFAULT_PATHS = ["src", "tests", "scripts", "packaging"]
CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
# служебные пометки инструментов — не комментарии для людей
DIRECTIVES = re.compile(r"^\s*(noqa|type:|pragma|fmt:|ruff:|pyright:|pylint:|isort:|!)", re.I)
TODO = re.compile(r"\b(TODO|FIXME|XXX)\b[:\s]*(.*)", re.I)
# то, что законно остаётся латиницей: адреса, код в `...`, «цитаты», идентификаторы
CODE_LIKE = re.compile(
    r"https?://\S+|`[^`]*`|«[^»]*»|\"[^\"]*\"|'[^']*'|\S*[_./\\()=<>\[\]{}:@#$%*+]\S*|"
    r"\b[A-Za-z]+[A-Z][a-z]\w*\b|\b[A-Z0-9]{2,}\b"
)
LATIN_WORD = re.compile(r"\b[a-z]{3,}\b", re.I)


def is_english(text: str) -> bool:
    """Текст для людей без кириллицы, но с латинскими словами — значит, не на русском."""
    if CYRILLIC.search(text) or DIRECTIVES.match(text):
        return False
    return len(LATIN_WORD.findall(CODE_LIKE.sub(" ", text))) >= 2


def check_todo(text: str) -> str | None:
    match = TODO.search(text)
    if not match:
        return None
    description = match.group(2).strip()
    if len(description) < 15 or not CYRILLIC.search(description):
        return f"{match.group(1)} без внятного описания по-русски (что сделать и почему)"
    return None


def check_file(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    problems = []
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            text = tok.string.lstrip("#").strip()
            line = tok.start[0]
            if line == 1 and tok.string.startswith("#!"):
                continue
            if is_english(text):
                problems.append(f"{path}:{line}: комментарий не на русском: {tok.string.strip()}")
            todo = check_todo(text)
            if todo:
                problems.append(f"{path}:{line}: {todo}")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node)
            if doc and is_english(doc.split("\n\n")[0]):
                line = node.body[0].lineno if node.body else 1
                problems.append(f"{path}:{line}: docstring не на русском: {doc.splitlines()[0]}")
    return problems


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    targets = [Path(a) for a in argv] or [root / p for p in DEFAULT_PATHS]
    files = [f for t in targets for f in ([t] if t.is_file() else sorted(t.rglob("*.py")))]
    problems = [p for f in files if ".venv" not in f.parts for p in check_file(f)]
    for problem in problems:
        print(problem)
    if problems:
        print(f"Нарушений правил комментирования: {len(problems)} (docs/rules/comments.md)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
