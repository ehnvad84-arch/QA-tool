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

---

# Проверка багфикса: f04a81d..839b035

**Вердикт:** fail (1 major: находка 26)

Дифф f04a81d..839b035, 2026-11-04, code-reviewer (Claude Opus 5.5). Всё, что выше, — прошлая проверка, она не менялась. Применимый фреймворк — только OWASP Top 10. Номера находок — как в review.md.

## Базовый минимум

- [x] В диффе нет секретов: поиск по диффу ничего не нашёл, в тестах только `admin` и `wrong` виртуальной камеры.
- [x] `.env` не менялся; `.gitignore`, `.cursorignore`, `.claude/settings.json` в диапазоне нет.
- [x] Нет доступа к продакшну: прод-ключей и команд деплоя в диффе нет.
- [x] Минимальные права: новых сервисов и токенов нет; посредник слушает только 127.0.0.1.
- [ ] Сканеры запущены — частично (ниже).

### Правила проекта и обязательные проверки

| Правило | Результат | Где |
|---|---|---|
| Табу AD-7 не ослаблено | pass | `core/taboo.py`, `never.md`, `architecture.md`, `tests/test_architecture.py`, `http.py` в диапазоне не менялись. `rtsp_probe` шлёт только OPTIONS и живёт в разрешённом `probe.py`. Обязательная CORE-CONNECT-1 только читает. Новых запросов к камере нет. Исключения onvif-tt ничего не разрешают |
| Новый клиент к камере — через `core/taboo.py` | fail (major) | Новых клиентов нет. Обход существующего посредника через `Location` не закрыт — taboo_proxy.py:131-140, 218-219 (находка 26) |
| Пароль не в журналах, командной строке, доказательствах, протоколах | pass, с замечанием | Новый код пароль не трогает. `rtsp_probe` без учётных данных. В причину CORE-CONNECT-1 идёт только текст камеры, а она видит дайджест, не пароль. `credentials_rejected` ничего не пишет. Условная утечка: находка 26 при откате onvif-tt на PasswordText |
| Вырезание Authorization, UsernameToken, cookie | pass | `core/secrets.py` не менялся; `Exchange` идёт через `redact` |
| Ответы камер — недоверенные данные | pass, с замечанием | Токен профиля экранируется (22 закрыта). Свободный текст причины влияет на классификацию отказа (находка 27, minor) |
| Только адреса стенда | fail (major) | Находка 26 |

## Сканеры

| Инструмент | Команда | Результат |
|---|---|---|
| Snyk — зависимости (SCA) | `snyk test` | не запускался: snyk не установлен. По security.md п. 5 заменён на pip-audit |
| Snyk — код (SAST) | `snyk code test` | не запускался: snyk не установлен |
| pip-audit | CI, задание `dependencies` | локально не запускался: инструмента нет, `pyproject.toml` и `uv.lock` в диапазоне не менялись |
| ruff | `uv run ruff check .` | чисто |
| agentshield | `agentshield scan --path .` | не запускался: хуки, MCP, `.claude/`, скиллы и плагины в диапазоне не менялись |
| Claude Code Security / `/security-review` | запускает главная сессия | — |

## OWASP Top 10 (редакция 2025)

| Пункт | Результат | Где |
|---|---|---|
| A01 Broken Access Control | fail (major) | Обход посредника табу: `Location: http://@хост/`, `//@хост/`, хост с `_` или не латиницей — находка 26 |
| A02 Security Misconfiguration | pass | Настройки посредника и журналов не менялись |
| A03 Software Supply Chain Failures | pass | Зависимости не менялись |
| A04 Cryptographic Failures | pass | Изменений нет. Откат на PasswordText в `SoapClient` не добавлять без решения директора (находка 24) |
| A05 Injection | pass | Находка 22 закрыта: `escape(profile)` |
| A06 Insecure Design | fail (minor) | Классификация отказа по свободному тексту камеры — находка 27. Управлять ею может только сама камера, и итог — ошибка стенда, а не ложная «прошла» |
| A07 Authentication Failures | fail (minor) | Отказ не по паролю считается неверным паролем (находка 24, в review.md — blocker качества итогов, не уязвимость); `LOCAL-AUTH-WRONG-PASSWORD-REJECTED` исключён, цена записана в спеке |
| A08 Software or Data Integrity Failures | pass | Изменений нет |
| A09 Logging and Alerting Failures | fail (minor) | Нет обмена SOAP в доказательствах при отказе — находка 28; потеря итога возврата при обрыве — находка 29 |
| A10 Mishandling of Exceptional Conditions | fail (minor) | IndexError (20) закрыт. `CameraUnreachable` при включении записи — находка 29 |

## Находки

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| 26 | major | src/qa_tool/drivers/camera/taboo_proxy.py:131-140, 218-219 | `Location` с пустым пользователем, хостом с `_` или не латиницей не подменяется; на `@` проверено: POST с телом уходит мимо посредника | Разбирать через `urlsplit` и `urljoin`, всё, кроме хоста камеры, — на `/_foreign` |
| 27 | minor | src/qa_tool/drivers/camera/soap.py:70-73 | Подстрока в тексте причины от камеры снимает всю камеру с прогона | Сравнивать поля `code` и `status` |
| 28 | minor | src/qa_tool/checks/core/connect.py:60-68 | Нет доказательства обмена при отказе | Собрать `evidence` до ветки отказа |
| 29 | minor | src/qa_tool/checks/onvif/replay.py:138 | При `CameraUnreachable` итог возврата режимов теряется | Ловить `(CameraUnreachable, OnvifFault)` |
