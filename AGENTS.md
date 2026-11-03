# QA-tool — стенд проверки IP-камер

<!-- Этот файл описывает проект «как есть», не больше 200 строк. Правила «как надо» — в docs/rules/.
Как устроены эти файлы — .workflow/WORKFLOW.md, раздел «Где что живёт». -->

## WHAT — стек и структура

- Стек: Python 3.12, PySide6 (Qt) — окно; onvif-tt + zeep — ONVIF; schemathesis + httpx — API; Playwright — веб-интерфейс; ffmpeg/ffprobe — поток и кодеки; OpenCV, scikit-image — картинка; SQLite — результаты; openpyxl, Qt PDF — протоколы.
- Сборки: Windows (PyInstaller + Inno Setup) и Linux (AppImage + .deb) из одного кода.
- Код ещё не написан — каркас появится на этапе 1 (docs/ROADMAP.md). Целевая карта:
  - `src/qa_tool/core/` — модели, сценарий прогона, планировщик на 8 камер, блокировки ресурсов стенда, база результатов
  - `src/qa_tool/checks/<модуль>/` — проверки: onvif, api, web, io, lens, daynight, stream, codec, sdcard, image, analytics, compare
  - `src/qa_tool/drivers/` — `camera/` (ruver, generic_onvif) и `stand/` (relay, gpio, audio, light, screen, poe)
  - `src/qa_tool/report/` — протокол PDF/Excel/HTML, таблица задач программистам, отчёт сравнения
  - `src/qa_tool/gui/` — экраны, подсказки, F1
  - `tests/` — тесты самой программы (mock-камера, записанные ответы камер)
  - `packaging/` — установщики
- `docs/` — PRD, ROADMAP, оборудование стенда (`docs/stand/`), руководства (они же справка F1), правила (`docs/rules/`), рабочие папки фич (`docs/work/<slug>/`)
- `.workflow/` — процесс разработки. Не редактировать: обновляется из Vadims workflow.

## WHY — назначение

Программа для ОТК и разработчиков «Рувер»: проверяет до 8 IP-камер одновременно на стенде по всему функционалу и выдаёт протокол по каждой камере с задачами программистам, плюс сравнение камер между собой (свои модели, прошивки и конкуренты).

- ONVIF — соответствие профилям S/G/M/T, включая реальную выдачу архива
- API и веб «Рувер» — каждая команда и каждая кнопка работает и меняет то, что должна
- стенд (реле, звук, свет, экран) — физические интерфейсы, день/ночь, ИК, объектив, аналитика
- картинка и кодеки — настройка даёт ожидаемый эффект; качество на битрейт
- протокол — документ для ОТК и список задач для программистов

## HOW — команды

Появятся на этапе 1. Ожидаемые:

- Установка: `uv sync`
- Запуск окна: `uv run qa-tool`
- Прогон без окна: `uv run qa-tool run --stand stand.yaml`
- Тесты: `uv run pytest` · один тест: `uv run pytest tests/path::test_name`
- Линтер и форматирование: `uv run ruff check . && uv run ruff format --check .`
- Виртуальные камеры для ручной проверки: `uv run python -m tests.mock_camera --count 9 --write-stand stand.yaml` (логин и пароль admin)
- Qt (окно, PDF) на Linux требует `libegl1`. На машине разработки без него: `apt-get download libegl1`, распаковать `dpkg-deb -x` в `~/.cache/qa-tool-syslibs/root` и запускать с `LD_LIBRARY_PATH=~/.cache/qa-tool-syslibs/root/usr/lib/x86_64-linux-gnu`

## Внешние источники

- ONVIF-проверки — пакет onvif-tt (github.com/OpenIPC/onvif-tt), закреплённая версия.
- Каталог тест-кейсов ONVIF и матрица профилей — github.com/onvif/testspecs (DocBook XML).
- Справочник операций и mock-камера — github.com/smiti1642/oxvif.
- API камеры «Рувер» — OpenAPI `ruware-api-release-*.json` из репозитория прошивки (~/projects/HiSi-Developing-Firmware); веб-интерфейс — там же, `www/` (Vue 3).

## Правила проекта

Прочитай в начале сессии, если их ещё нет в контексте: docs/rules/conventions.md, docs/rules/architecture.md, docs/rules/never.md, docs/rules/security.md.

## Как работаем

Процесс описан в .workflow/WORKFLOW.md. Целиком его не читай: открой docs/work/<slug>/plan.md и раздел «Handoff» в progress.md, определи этап и прочитай только его раздел.
Новая фича: `.workflow/feature.sh <slug>`.
