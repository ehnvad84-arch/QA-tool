# Security-check: skeleton

**Вердикт:** fail. Blocker нет: все fail ниже на уровне warning, подробности в review.md, раздел «Находки».

Ревьюер: независимый ревьюер (Claude Opus 5.5) · Дифф: main...feature/skeleton (до f25467d) · Дата: 2026-11-03
Применимый фреймворк по docs/rules/security.md: только OWASP Top 10. LLM Top 10, Agentic AI, MITRE ATLAS и NIST AI RMF не отмечены и не проверялись.

## Базовый минимум

- [x] Секретов в диффе и истории ветки нет. Поиск по `git log -p main..feature/skeleton` нашёл только тестовые значения (`admin`, `s3cret`, `bad`). Пароли mock-камер — тестовые, это нормально.
- [x] `.env` не в индексе. Всё на месте: `.gitignore` (`.env`, `.env.*`, `!.env.example`), `.cursorignore`, deny `Read(./.env)` и `Read(./.env.*)` в `.claude/settings.json`. В `.env.example` только имена `QA_TOOL_PASSWORD_1..8`.
- [x] Доступа к продакшну нет: прод-ключей, прод-адресов и команд деплоя нет. CI собирает артефакты, но не публикует их.
- [ ] Права минимальны не везде: у `ci.yml` нет блока `permissions:`, права токена берутся по умолчанию (N12).
- [ ] Сканеры не запускались: Snyk не установлен, задание CI без токена тихо пропускается (W11).

## Сканеры

| Инструмент | Команда | Результат |
|---|---|---|
| Snyk — зависимости (SCA) | `snyk test` | не запускался: Snyk на машине не установлен; в CI задание без `SNYK_TOKEN` завершается кодом 0 |
| Snyk — код (SAST) | `snyk code test` | не запускался: Snyk не установлен |
| agentshield | `agentshield scan --path .` | не нужен: `.claude/`, хуки, MCP, скиллы и плагины в диффе не менялись |
| Claude Code Security / `/security-review` | запускает главная сессия | — |

## Правила проекта (security.md, «Правила этого проекта»)

| Правило | Результат | Где |
|---|---|---|
| Табу: каждый клиент к камере идёт через `core/taboo.py` | fail | Через табу идёт только `SoapClient.call` (soap.py:69). Сокеты в connect.py:12-28 и worker.py:41 открываются напрямую (это чтение). Единой точки и проверки, что её не обходят, нет (W4). `check_web_request` пропускает GET-запросы Dahua и Axis, которые меняют сеть и пароли (W1). У `check_http` есть обходы через формат пути (W2). Список ONVIF неполон, разбор хрупкий (W3) |
| Пароли только в keyring, не в stand.yaml, базе и протоколе | pass | config.py (поле `password` отклоняется), store.py (колонки пароля нет), secrets.py; мелочи — N2 и N11 |
| В протоколах и доказательствах вырезаются Authorization, UsernameToken, cookie | pass | secrets.py:30-48, soap.py:83-88 (известный пароль плюс шаблоны), store.py:96, 113-115, report/model.py; узкие шаблоны — N3 |
| Ответы камер экранируются, в путях не используются без очистки | fail | HTML: autoescape (report/html.py:9), тест с `<script>`; пути: `safe_name`. Excel: formula injection (W5). Зарезервированные имена Windows — N4 |
| Только адреса из списка стенда; сканирование — WS-Discovery в своей подсети | pass с замечаниями | TTL 2 вместо 1, адрес берётся из XAddrs (N5); XAddrs из GetCapabilities пока не используются (N15) |
| Проверки защищённости (скан, EMBA) — только на стенде | n/a | в каркасе их нет |

## OWASP Top 10 (редакция 2025)

| Пункт | Результат | Где |
|---|---|---|
| A01 Broken Access Control (включая SSRF) | pass | Настольная программа без пользователей. Запросы идут только на адреса со стенда. Замечания: адрес из XAddrs поиска не сверяется с отправителем (N5), XAddrs из GetCapabilities позже понадобится сверять (N15) |
| A02 Security Misconfiguration | pass | XML через defusedxml (soap.py, discovery.py), Jinja2 `autoescape=True`, справка с `setOpenExternalLinks(False)`. Замечание: нет `permissions:` в CI (N12) |
| A03 Software Supply Chain Failures | недостаточно данных | Есть `uv.lock`; ffmpeg, appimagetool и runtime закреплены по SHA-256 (packaging/fetch.py:17-38, 58-65); у appimagetool хэш первой загрузки (TOFU, записано в progress.md). Snyk по зависимостям не запускался (W11) |
| A04 Cryptographic Failures | pass | Пароли хранятся только в системном хранилище. ONVIF — PasswordDigest с nonce (стандарт ONVIF, SHA-1). Транспорт HTTP без TLS — ограничение протокола камер в сети стенда |
| A05 Injection | fail | Excel: строки от камеры с `=` становятся формулами (xlsx.py:54, W5). HTML экранируется, SQL параметризован (store.py; в `set_passport` имена полей — константы). Логин в XML не экранируется (soap.py:60, N6, данные вводит сам инженер) |
| A06 Insecure Design | fail | Защита табу AD-7 по замыслу пропускает GET (W1), нормализация пути слабая (W2), операция ONVIF определяется по первому тегу, а список неполон (W3), единой точки контроля нет (W4) |
| A07 Identification and Authentication Failures | pass | Пароли не попадают в файлы, для CI — переменные окружения. Замечание: ключ хранилища по одному адресу (N2) |
| A08 Software or Data Integrity Failures | pass | Скачиваемые инструменты сборки проверяются по SHA-256, zip и tar распаковываются безопасно (`filter="data"`). Замечание: actions не закреплены по SHA (N12) |
| A09 Security Logging and Alerting Failures | pass | Нарушение табу превращается в «ошибку стенда» с причиной и попадает в протокол (checks.py:163-164). Замечание: вывод причины в консоль и текст исключения не очищаются известным паролем (N3) |
| A10 Mishandling of Exceptional Conditions | fail | Окно зависает в «идёт прогон», CLI отдаёт код 1 вместо 2 при ошибке не из конфигурации (W6). Чужой пакет WS-Discovery роняет поиск (W7) |

## Находки

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| W1 | warning | src/qa_tool/core/taboo.py:161, 170-179 | GET не проверяется. Нет признаков `configurationdata` и `activate`. Веб-обход камеры без адаптера сменит сеть или пароль | Проверять любой метод с параметрами изменения; дополнить признаки; тесты с настоящими запросами Dahua, Axis и Hikvision |
| W2 | warning | src/qa_tool/core/taboo.py:76-85 | Полный URL, `//`, `./` и `%XX` обходят запрещённые префиксы API «Рувер» | `urlsplit` + `unquote` + `normpath` |
| W3 | warning | src/qa_tool/core/taboo.py:16-46, 59-73 | Нет операций хеширования паролей, политик паролей, сертификатов и TLS. Комментарий или полный конверт обходят определение операции | Разбор через defusedxml, проверка всех элементов `Body`, разрешительный список |
| W4 | warning | connect.py:12-28, worker.py:41, архитектура | Через табу идёт только `SoapClient`. Обойти его могут любая проверка и onvif-tt (zeep) | Архитектурный тест на импорты сетевых библиотек; хук для zeep |
| W5 | warning | src/qa_tool/report/xlsx.py:54 | Formula injection из ответов камеры в протокол ОТК | `data_type = "s"` или префикс `'` для `= + - @ \t \r` |
| W6 | warning | gui/screens/run.py:155, cli.py:65, core/config.py:25, 61 | Необработанные исключения: окно зависает, CLI даёт неверный код выхода | Ловить `Exception`, возвращать код 2 |
| W7 | warning | drivers/camera/discovery.py:63, 99; gui/screens/cameras.py:121 | Любое устройство в подсети роняет поиск (ValueError, EntitiesForbidden) | Ловить ValueError на каждый пакет |
| W11 | warning | .github/workflows/ci.yml:38-41 | Snyk тихо пропускается, требование security.md п. 5 не выполняется | Падать без токена на PR в `main`; прогнать Snyk до мержа |
| N2–N6, N11, N12, N15 | nit | см. review.md | Мелкие замечания по секретам, именам файлов, поиску, CI | см. review.md |

---

## Повторное ревью (после багфикса)

**Вердикт:** fail без blocker. A06 провален на уровне warning: R1 и R2 в review.md. Правило security.md п. 5 (Snyk на каждом PR) не выполнено, это W11.

Ревьюер: независимый ревьюер (Claude Opus 5.5) · Дифф: `d24811c~1..HEAD` · Дата: 2026-11-03

### Базовый минимум

- [x] Секретов в диффе нет: только тестовые `s3cret`, `admin`, `abc123`, `pw`.
- [x] `.env` не в индексе, deny в `.claude/settings.json` на месте.
- [x] Доступа к продакшну нет.
- [x] Права в CI минимальны: `permissions: contents: read` (N12). Actions по-прежнему закреплены тегами, а не SHA.
- [ ] Сканеры: Snyk не установлен и не запускался; в CI без `SNYK_TOKEN` он по-прежнему пропускается. pip-audit 2.10.1 запущен локально по `uv export --no-dev` — «No known vulnerabilities found».

### Сканеры

| Инструмент | Команда | Результат |
|---|---|---|
| Snyk — зависимости | `snyk test` | не запускался: Snyk не установлен |
| Snyk — код | `snyk code test` | не запускался: Snyk не установлен |
| pip-audit | `uvx pip-audit@2.10.1 --strict -r <uv export>` | уязвимостей не найдено |
| agentshield | `agentshield scan --path .` | Grade C (63/100), 81 находка (14 critical), все про «prompt defense» в `.claude/rules/*.md` (симлинк на `docs/rules`); новый git-хук и код в находках не упомянуты |

### Правила проекта

| Правило | Результат | Где |
|---|---|---|
| Табу: каждый клиент к камере идёт через единую точку | pass с замечаниями (warning) | `http.py` + `probe.py` + тест архитектуры. Обходы прошлого ревью закрыты. Новые: SOAP-ветка пропускает проверку пути и слов (R1); тест архитектуры не видит `onvif`, `from urllib import request`, реэкспорт `httpx` (R2) |
| Пароли только в keyring | pass | ключ «адрес:порт» (N2); stand.yaml проверяется и на верхнем уровне (N11) |
| В протоколах и доказательствах вырезаются секреты | pass | шаблоны шире, `redact` в `run_check`, CLI и окне; остаток N3 (`token: …`, `api_key=`, `PHPSESSID=`) |
| Ответы камер экранируются | pass | Excel: строки — тип `s`, тегов `<f>` в XML нет (W5); логин в XML экранируется (N6); зарезервированные имена Windows (N4) |
| Только адреса стенда; WS-Discovery в своей подсети | pass с замечаниями | TTL 1; хост XAddrs с отправителем не сверяется (N5), N15 открыта |

### OWASP Top 10 (2025)

| Пункт | Результат | Где |
|---|---|---|
| A01 Broken Access Control | pass | без изменений; N5 (частично) и N15 |
| A02 Security Misconfiguration | pass | `permissions` в CI; protocols открывает базу только для чтения |
| A03 Software Supply Chain Failures | pass с замечаниями | pip-audit в CI и локально чист; `uv sync --locked`; Snyk не запускался, actions не по SHA |
| A04 Cryptographic Failures | pass | без изменений |
| A05 Injection | pass | формулы Excel и логин в XML закрыты (W5, N6) |
| A06 Insecure Design | fail (warning) | R1: проверка в единой точке выбирается по эвристике SOAP, путь при этом не проверяется. R2: тест архитектуры обходится. Мелочи: R3–R6 |
| A07 Identification and Authentication Failures | pass | N2 закрыта |
| A08 Software or Data Integrity Failures | pass | без изменений |
| A09 Security Logging and Alerting Failures | pass | очистка паролем в `run_check` и CLI (N3); код выхода процесса в причине |
| A10 Mishandling of Exceptional Conditions | pass с замечаниями | W6 и W7 закрыты. Остаток R8: YAML не со словарём даёт AttributeError; `SoapClient.call` на DTD в ответе выдаёт ValueError вместо OnvifFault (C10, старый долг) |

### Находки

| # | Уровень | Где | Проблема | Фикс |
|---|---|---|---|---|
| R1 | warning | src/qa_tool/drivers/camera/http.py:15-17, 47-51 | `Envelope` в теле или `soap` в Content-Type отключают `check_http` и `check_web_request`; ONVIF после длинного префикса минует `check_onvif` | `check_http` всегда, `check_onvif` для любого XML, словами судить только не-SOAP |
| R2 | warning | tests/test_architecture.py:8-11 | Не ловятся `onvif`, `from urllib import request`, `from http import client`, `import os, socket`, реэкспорт `httpx`, playwright | Разбор через `ast`, расширить список |
| W11 | warning (не новое) | .github/workflows/ci.yml | Snyk без токена пропускается; security.md п. 5 не выполнено | Токен или решение директора о замене на pip-audit |
| R3–R6 | nit | src/qa_tool/core/taboo.py | Остаточные обходы и ложные срабатывания | см. review.md |
