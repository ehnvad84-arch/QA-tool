# Security-check: onvif

**Вердикт:** fail

Дифф main...feature/onvif (до e732a45), 2026-11-04, code-reviewer (Claude Opus 5.5). Применимый фреймворк по docs/rules/security.md — только OWASP Top 10; OWASP LLM Top 10 и Agentic AI Threats не отмечены, поэтому их разделы удалены.

## Базовый минимум

- [x] В диффе и истории ветки нет секретов: поиск по `git log main..feature/onvif -p` ничего не нашёл. В тестах только учётные данные виртуальной камеры `admin`/`admin` и маркер `s3cret-pw` в тесте.
- [x] `.env` не в индексе (в индексе только `.env.example`); `.env` и `.env.*` закрыты в `.gitignore` и `.cursorignore`, deny `Read(./.env)` и `Read(./.env.*)` в `.claude/settings.json` на месте.
- [x] Нет доступа к продакшну: прод-ключей, прод-адресов, команд деплоя нет.
- [x] Минимальные права: посредник слушает только 127.0.0.1 на случайном порту и живёт на время модуля одной камеры. `QA_TOOL_OTT_PASSWORD` получает только дочерний процесс; запускатель удаляет её из своего окружения (`os.environ.pop`). Начальное окружение процесса читают только тот же пользователь и root — риск принят решением спеки.
- [ ] Сканеры запущены — частично (ниже).

### Правила проекта (security.md)

| Правило | Результат | Где |
|---|---|---|
| Новый клиент к камере — через `core/taboo.py` | fail | ONVIF наших проверок — через `SoapClient`/`http.request`: pass. RTSP наших проверок — `rtsp.py` с `check_rtsp` на каждый метод, включая TEARDOWN в `close()`: pass. onvif-tt по HTTP — через `TabooProxy`: pass, кроме XAddr с `HTTP://` (review.md, находка 2). RTSP onvif-tt (ffprobe) идёт мимо `check_rtsp` (находка 1) |
| Пароли — только keyring/окружение, не в stand.yaml, базе, протоколе | fail | В stand.yaml, базе и протоколе пароля нет (`redact`, `RtspClient.exchanges` без Authorization и логина в адресе, тест `rtsp.txt` без `admin`). Но onvif-tt передаёт `user:пароль@` в аргументах ffprobe (находка 1) |
| Вырезание Authorization, UsernameToken, cookie из доказательств | pass | `core/store.py:119` — `redact` на все текстовые доказательства; шаблоны `Authorization:`, `<wsse:Password>`, `scheme://user:pass@` покрывают `last_request` onvif-tt, `sdp.txt`, `rtsp.txt`, `onvif-*.txt`, адрес NAS (`storage.py:68`) |
| Ответы камер — недоверенные данные | pass, с замечаниями | HTML: `autoescape=True`; Excel: строки принудительно текст; XML — defusedxml (`soap.py`, `metadata.py:129`); имена файлов — `safe_name`. Замечания: неэкранированные токены в SOAP (находка 8), неограниченный размер ответов RTSP (находка 7) |
| Только адреса стенда | fail | `service_urls` отбрасывает чужие XAddr; посредник подменяет их на `/_foreign`; `to_stand_address` для GetStreamUri и GetReplayUri: pass. Не закрыто: RTSP onvif-tt по адресу из GetStreamUri (находка 1); `HTTP://` в XAddr (находка 2); путь посредника без `/` (находка 6) |

## Сканеры

| Инструмент | Команда | Результат |
|---|---|---|
| Snyk — зависимости (SCA) | `snyk test` | не запускался: snyk не установлен. По security.md п. 5 заменён на pip-audit (решение директора 2026-11-03) |
| Snyk — код (SAST) | `snyk code test` | не запускался: snyk не установлен |
| pip-audit | `uvx pip-audit@2.10.1 --strict -r audit.txt` | не запускался: запуск внешнего пакета через uvx запрещён правами сессии ревью. В CI задание `dependencies` есть; onvif-tt (git) из проверки исключён и закреплён коммитом 8866b251 — его код просмотрен в части сетевых вызовов (ffprobe, `urllib` только в `schema_store.refresh`, который при прогоне не вызывается) |
| ruff (вкл. S324 с обоснованными noqa для MD5 Digest) | `uv run ruff check .` | чисто |
| agentshield | `agentshield scan --path .` | `.claude/`, MCP, скиллы и плагины в диффе не менялись (изменён только git-хук `.githooks/pre-push`); общий скан репозитория: C 63/100, 14 critical — все о защитных формулировках в `CLAUDE.md` и `.claude/rules/*`, вне диффа |
| Claude Code Security / `/security-review` | запускает главная сессия | — |

## OWASP Top 10 (редакция 2025)

| Пункт | Результат | Где |
|---|---|---|
| A01 Broken Access Control | fail (major) | Обход посредника табу через `HTTP://` в XAddr — `taboo_proxy.py:205-207` (находка 2); ретрансляция на чужой хост путём без `/` — `taboo_proxy.py:104-112` (находка 6, minor). Привязка к 127.0.0.1 и случайный порт — pass |
| A02 Security Misconfiguration | pass | Посредник без журнала запросов (`log_message`), `daemon_threads`; журнал httpx приглушён; pytest запускателя с `PYTEST_DISABLE_PLUGIN_AUTOLOAD` и пустым `pytest.ini` |
| A03 Software Supply Chain Failures | pass | onvif-tt по полному SHA в `pyproject.toml` и `uv.lock`, обновление — через ревью (AD-6); NOTICE с лицензиями; ffmpeg CI — закреплённая сборка `packaging/fetch.py`. pip-audit локально не запускался (см. выше) |
| A04 Cryptographic Failures (раскрытие секретов) | fail (blocker) | Пароль камеры в аргументах ffprobe из onvif-tt и передача учётных данных на хост из GetStreamUri — `ott_runner.py:370-382`, onvif-tt `cases/media.py:245-260` (находка 1). MD5 в Digest — требование RFC 2617, не слабость реализации |
| A05 Injection | fail (minor) | Значения из ответов камеры без XML-экранирования в телах SOAP — `replay.py`, `metadata.py`, `snapshot.py` (находка 8); табу не обходится, потому что `check_onvif` проверяет итоговое тело. Заголовки RTSP: значения из ответа камеры очищены от CR/LF через `readline().strip()` — pass. HTML и Excel — pass |
| A06 Insecure Design | pass | Отдельный процесс для onvif-tt, пароль через окружение, посредник на время модуля, сокеты только в разрешённых файлах; дыра теста архитектуры для `http.server`/`socketserver` — находка 13 (minor) |
| A07 Authentication Failures | pass | RTSP: Basic — только по вызову камеры и только в сокет камеры; Digest с qop/opaque; один повтор на 401 |
| A08 Software or Data Integrity Failures | pass | Отчёт onvif-tt — JSON из своего временного каталога; обязательные функции профилей сверены тестом с файлом спецификации |
| A09 Logging and Alerting Failures | pass | Остановленные табу запросы — `TabooProxy.blocked` и причина строки «запрещённая операция не отправлена (табу)»; журнал RTSP без учётных данных |
| A10 Mishandling of Exceptional Conditions | fail (minor) | Неограниченные `readline`/`read(Content-Length)` и `struct.error` в `rtsp.py:197-206` (находка 7); IndexError на неожиданных ответах (находка 9); UnicodeEncodeError в посреднике (находка 10) |

## Находки

Номера — как в review.md.

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| 1 | blocker | src/qa_tool/checks/onvif/ott_runner.py:370-382 | onvif-tt-тесты `requires_ffprobe` запускают ffprobe с `user:пароль@` в аргументах (виден в списке процессов) и по хосту из GetStreamUri без подмены (учётные данные уходят на чужой хост). Условие — ffprobe в PATH (Linux с ffmpeg, CI) | Исключить тесты `requires_ffprobe` в Excluder/`excluded.py` или убрать ffprobe из PATH запускателя; видеопоток подтверждать своими проверками на `rtsp.py` |
| 2 | major | src/qa_tool/drivers/camera/taboo_proxy.py:205-207 | `HTTP://` в XAddr не подменяется — onvif-tt ходит мимо посредника табу | `re.IGNORECASE` и `scheme.lower()` |
| 6 | minor | src/qa_tool/drivers/camera/taboo_proxy.py:104-112 | Путь без `/` (`@host/…`) пересылается на произвольный хост | Отказ, если путь не начинается с `/`; сверка хоста перед отправкой |
| 7 | minor | src/qa_tool/drivers/camera/rtsp.py:197-206 | Размер ответа RTSP не ограничен | Пределы для `readline` и `Content-Length` |
| 8 | minor | replay.py, metadata.py, snapshot.py (строки в review.md) | Значения камеры без экранирования в SOAP | `xml.sax.saxutils.escape` |
| 13 | minor | tests/test_architecture.py:12-34 | `http.server`, `socketserver`, `asyncio` не в `NETWORK_MODULES` | Добавить; `taboo_proxy.py` — в ALLOWED |
