"""QA-tool — стенд проверки IP-камер."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("qa-tool")
except PackageNotFoundError:  # запуск из исходников без установки
    __version__ = "0.0.0"
