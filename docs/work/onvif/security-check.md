# Security-check: onvif

**Вердикт:** fail

Повторная проверка: дифф c09f377..f04a81d, 2026-11-04, code-reviewer (Claude Opus 5.5). Применимый фреймворк по docs/rules/security.md — только OWASP Top 10; OWASP LLM Top 10 и Agentic AI Threats не отмечены, поэтому их разделы удалены. Прежняя редакция файла — в истории Git (c09f377).

## Базовый минимум

- [x] В диффе нет секретов: поиск по `git log c09f377..HEAD -p` ничего не нашёл. В тестах только `admin` виртуальной камеры; в progress.md — адрес камеры в локальной сети, без учётных данных.
- [x] `.env` не в индексе (только `.env.example`). `.gitignore`, `.cursorignore` и `.claude/settings.json` в диапазоне не менялись.
- [x] Нет доступа к продакшну: прод-ключей, прод-адресов и команд деплоя в диффе нет.
- [x] Минимальные права: новых сервисов и токенов нет; посредник по-прежнему слушает только 127.0.0.1.
- [ ] Сканеры запущены — частично (ниже).

### Правила проекта и обязательные проверки

| Правило | Результат | Где |
|---|---|---|
| Табу AD-7 не ослаблено | pass | `core/taboo.py`, `docs/rules/never.md`, `tests/test_architecture.py` в диапазоне не менялись. Дополнение AD-7 только описывает `rtsp.py` (методы из `RTSP_READ_METHODS`, `check_rtsp`) и `taboo_proxy.py` (127.0.0.1, пересылка через `http.py`), что совпадает с кодом и `ALLOWED`. Экранирование не обходит табу: `check_onvif` проверяет готовое тело |
| Новый клиент к камере — через `core/taboo.py` | fail (major) | Новых клиентов нет, B1 и M2 закрыты. `Location: //хост` уводит onvif-tt мимо посредника — taboo_proxy.py:131-133 (находка 18) |
| Пароль не в журналах, командной строке, доказательствах, протоколах | pass | Командная строка onvif-tt: только `--target`, `--user`, `--report`, `--exclude`, флаги (`onvif_tt.py:153-167`), пароль в окружении дочернего процесса. ffprobe-тесты исключены, PATH без ffprobe. `Password` с атрибутом `Type` вырезает шаблон `secrets.py:53`; новый атрибут `EncodingType` у Nonce этого не меняет, а Nonce без дайджеста секретом не является. Посредник журнал не ведёт |
| Вырезание Authorization, UsernameToken, cookie | pass | `core/secrets.py` не менялся; `SoapClient` и `RunStore` по-прежнему вызывают `redact` |
| Ответы камер — недоверенные данные | pass, с замечанием | Значения экранируются в `replay.py`, `metadata.py`, `snapshot.py`; `stream.py:56` — нет (находка 22, minor) |
| Только адреса стенда | fail (major) | `service_urls` и `/_foreign` работают, `Location` со схемой подменяется; `//хост` — нет (находка 18) |

## Сканеры

| Инструмент | Команда | Результат |
|---|---|---|
| Snyk — зависимости (SCA) | `snyk test` | не запускался: snyk не установлен. По security.md п. 5 заменён на pip-audit (решение директора 2026-11-03) |
| Snyk — код (SAST) | `snyk code test` | не запускался: snyk не установлен |
| pip-audit | CI, задание `dependencies` | локально не запускался: `pyproject.toml` и `uv.lock` в диапазоне не менялись |
| ruff | `uv run ruff check .` | чисто |
| agentshield | `agentshield scan --path .` | не запускался: хуки, MCP, `.claude/`, скиллы и плагины в диапазоне не менялись |
| Claude Code Security / `/security-review` | запускает главная сессия | — |

## OWASP Top 10 (редакция 2025)

| Пункт | Результат | Где |
|---|---|---|
| A01 Broken Access Control | fail (major) | `Location: //хост` — обход посредника табу, taboo_proxy.py:131-133 (находка 18). `HTTP://` и путь без `/` закрыты |
| A02 Security Misconfiguration | pass | Настройки посредника и журналов не менялись |
| A03 Software Supply Chain Failures | pass | Зависимости не менялись; onvif-tt закреплён по SHA |
| A04 Cryptographic Failures | pass | B1 закрыта. URI типов WS-Security верны. SHA-1 в PasswordDigest требует сам стандарт |
| A05 Injection | fail (minor) | Токен профиля без экранирования — stream.py:56 (находка 22). Остальные значения экранируются |
| A06 Insecure Design | pass | Отдельный процесс onvif-tt, посредник, тест архитектуры закрывает `http.server`, `socketserver`, `asyncio` |
| A07 Authentication Failures | fail (minor) | Реализация клиента — pass. Проверка камеры на отказ при неверном пароле (`LOCAL-AUTH-WRONG-PASSWORD-REJECTED`) исключена без замены — excluded.py:45 (находка 21) |
| A08 Software or Data Integrity Failures | pass | Изменений нет |
| A09 Logging and Alerting Failures | fail (minor) | При отказе включения записи теряется «камера осталась изменённой» — replay.py:130-151 (находка 19) |
| A10 Mishandling of Exceptional Conditions | fail (minor) | IndexError в снимке — snapshot.py:190 (находка 20). `struct.error` и U+FFFD закрыты. Неверная классификация отказа по паролю — находка 16, в review.md как blocker качества итогов, не как уязвимость |

## Находки

Номера — как в review.md.

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| 18 | major | src/qa_tool/drivers/camera/taboo_proxy.py:131-133 | `Location: //хост` не подменяется; requests достраивает схему, и onvif-tt идёт к камере мимо табу или на чужой хост с WS-Security | Разбирать адрес через `urljoin` к адресу камеры и после этого подменять; тест на `//<камера>` и `//<чужой>` |
| 19 | minor | src/qa_tool/checks/onvif/replay.py:130-151 | При исключении итог возврата режимов теряется | Вернуть `problems` или включить их в `OnvifFault` |
| 20 | minor | src/qa_tool/checks/onvif/snapshot.py:103, 190 | IndexError на ответе без ImagingSettings | Ловить в `take()` |
| 21 | minor | src/qa_tool/checks/onvif/excluded.py:45-46 | Нет проверки, что камера отвергает неверный пароль | Своя RUV-проверка на чтение, после вопроса директору о блокировке учётной записи |
| 22 | minor | src/qa_tool/checks/onvif/stream.py:56 | Значение камеры без экранирования в SOAP | `escape(profile)` |
