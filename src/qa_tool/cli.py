"""Командная строка: `qa-tool` (окно), `qa-tool run`, `qa-tool discover`."""

import argparse

from qa_tool import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qa-tool", description="Стенд проверки IP-камер")
    parser.add_argument("--version", action="version", version=f"qa-tool {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
