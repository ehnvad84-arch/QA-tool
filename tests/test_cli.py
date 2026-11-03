import pytest

from qa_tool import __version__
from qa_tool.cli import main


def test_version_prints_package_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out
