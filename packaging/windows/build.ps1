# Сборка установщика Windows: PyInstaller (onedir) + ffmpeg LGPL + Inno Setup.
#   powershell -File packaging\windows\build.ps1   → dist\QA-tool-<версия>-setup.exe
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\..")

uv sync --group build --quiet
$version = uv run python -c "from qa_tool import __version__; print(__version__)"
$env:QA_FFMPEG_BIN = uv run python packaging/fetch.py ffmpeg-windows
uv run python packaging/make_icon.py build/icon
Copy-Item build/icon/icon.ico packaging/icon.ico -Force
uv run pyinstaller --noconfirm --clean --distpath build/pyinstaller --workpath build/work packaging/qa-tool.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }

$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "ISCC.exe" }
& $iscc "/DVersion=$version" packaging\windows\qa-tool.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup завершился с ошибкой" }
Write-Host "Готово: dist\QA-tool-$version-setup.exe"
