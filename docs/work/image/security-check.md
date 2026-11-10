# Security-check: image

Ревьюер: Claude (claude-opus-5-5), свежий контекст · Дифф фичи: `9d669b6..HEAD` (по существу `8d57a17..HEAD`: после слияния этапа 2) · Дата: 2026-11-10

**Вердикт:** pass <!-- fail — ни одного; есть warning и note ниже, табу AD-7 не нарушено; круг 2 (ce867a6) вердикт не меняет -->

Применимые фреймворки по `docs/rules/security.md`: только OWASP Top 10. LLM Top 10, Agentic AI, MITRE ATLAS, NIST AI RMF не отмечены — разделы удалены.

## Базовый минимум

- [x] В диффе и истории ветки нет секретов: в `src/`, `tests/`, `packaging/` — только тестовые пароли mock-камер (`admin`, `smoke-S3cret-pw` в `packaging/smoke_image.py:29` — нарочно отличается от логина, чтобы смоук искал утечку). Пароль камеры в SOAP — дайджестом, обмен в `SoapClient.exchanges` хранится через `redact` (`drivers/camera/soap.py:211`); RTSP — только в Authorization `RtspClient`; ffmpeg получает временный файл без адреса и пароля (`checks/image/frames.py:245`, тест `test_decoder_gets_only_a_temp_file_and_folder_is_removed`). Текст хода `progress` чистится `redact` в планировщике (`core/scheduler.py:196`).
- [x] `.env` не в индексе (`git ls-files .env .env.local` — пусто); `.gitignore:1-3`, `.cursorignore:1-3`, deny `Read(./.env)`, `Read(./.env.*)` в `.claude/settings.json:3` на месте.
- [x] Нет доступа к продакшну: прод-ключей, прод-адресов и команд деплоя в диффе нет. CI (`.github/workflows/ci.yml`) получил только шаги смоука на mock-камерах и `timeout-minutes`.
- [x] Минимальные права: новых сервисов и токенов нет. Запись в камеру — только `SetImagingSettings` полей белого списка, только при `ctx.allow_writes` (`checks/image/engine.py:220`), защищённые блоки `Exposure`/`IrCutFilter`/`Focus`/`ImageStabilization` сверяются со снимком до отправки (`drivers/camera/imaging.py:423-433`).
- [ ] Сканеры запущены — частично, см. ниже.

## Сканеры

| Инструмент | Команда | Результат |
|---|---|---|
| Snyk — зависимости (SCA) | `snyk test` | не запускался: Snyk не установлен и заменён на pip-audit (security.md, п. 5, решение 2026-11-03) |
| Snyk — код (SAST) | `snyk code test` | не запускался: то же |
| pip-audit (замена Snyk) | `uvx pip-audit` | локально не запускался: команда требует подтверждения, ночью дать некому. Новые зависимости ветки — `numpy>=2`, `lxml>=5` (`pyproject.toml`) — проверяет CI, задание `dependencies`, до мержа |
| agentshield | `agentshield scan --path .` | не запускался: команда требует подтверждения. `.claude/`, MCP, скиллы, плагины в диффе не менялись; изменился git-хук `.githooks/pre-push` (75e4c82 — перепрогон до трёх упавших тестов): прочитан глазами, сетевых обращений и обхода проверок нет, но падающий тест при повторном успехе пропускает пуш — это решение директора 08.11, записано в самом хуке |
| ffmpeg (версия в сборке) | — | pip-audit её не видит; по спеке ведётся вручную. В этой фиче ffmpeg получает поток камеры из файла — см. находку S-2 |
| Claude Code Security / `/security-review` | запускает главная сессия | — |

## Табу AD-7 и правила проекта

| Правило | Результат | Где |
|---|---|---|
| Любой запрос к камере — через `core/taboo.py` | pass | ONVIF Imaging — только `SoapClient.call` → `check_onvif` до отправки (`drivers/camera/soap.py:150`); RTSP — `RtspClient` (`frames.py:201`), только методы на чтение. Новых сокетов и HTTP-клиентов нет; `tests/test_architecture.py` в диффе не менялся (исключений не добавлено) и проходит |
| Сеть, логины, пароли не меняются | pass | `GetOptions`, `Get/SetImagingSettings`, `GetProfiles`, `GetStreamUri`, `GetVideoSources` не в `FORBIDDEN_ONVIF`; полный блок, присланный камерой, проходит `check_onvif` целиком: вложенный `SetUser` → `TabooViolation` (`tests/drivers/test_imaging.py::test_full_block_with_nested_set_user_is_taboo`) |
| Режим экспозиции не меняется | pass с оговоркой | запись проверки не трогает `Exposure` (белый список + сверка со снимком). Возврат (`imaging.py:504`) и возврат по журналу (`imaging.py:465`) отправляют полный исходный блок, в том числе `Exposure`, с `ForcePersistence=true` — это запись исходных значений, не изменение; см. review.md, W-6 |
| Без галочки «изменяющие» — ни одной записи | pass | `engine.py:220-222` (после ce867a6 — `:246-248`); тест `test_without_checkbox_nothing_is_written_but_state_is_recorded` (`imaging_writes == []`) |
| Пароль не повторяется после отказа | pass | `refused_password` в начале прохода и перед каждым снимком (`engine.py:178`, `frames.py:180`); отказ ONVIF — `Abort` без повтора (`engine.py:305-307`), полный блок после отказа входа не шлётся (`imaging.py:374-375`) |
| Ответы камеры в путях файлов не используются | pass | имена доказательств — из ID строки и `p000/p050/p100`; журнал — `journal_file` очищает адрес (`write_journal.py:62-64`); PDF подкладывает только файлы внутри папки прогона (`report/pdf.py:44-57`) |
| Пароли и токены вырезаны из доказательств | pass | `onvif-N.txt` — из уже вычищенных `SoapClient.exchanges`; `measure.json`, `state.txt`, `imaging.xml` — только значения настроек; смоук `packaging/smoke_image.py:109` ищет пароль во всех файлах прогона |

## OWASP Top 10 (2021)

| Пункт | Результат | Где |
|---|---|---|
| A01 Broken Access Control | pass | новых точек входа нет; `write_pdf(..., base_dir)` берёт картинки только внутри папки прогона (`report/pdf.py:52-54`, `is_relative_to`) |
| A02 Cryptographic Failures | pass | пароль — из `keyring`, в ONVIF — PasswordDigest, в журнале и `measure.json` его нет |
| A03 Injection | pass | токен источника — `escape()` (`imaging.py:255`); значения в XML — числа `_format`; HTML — Jinja2 `autoescape=True` (`report/html.py:9`), новые шаблоны `_image.html`, `image_compare.html` без `|safe`; Excel — все строки `data_type="s"` (`report/xlsx.py:60-63`), ссылки — пути программы; ffmpeg/ffprobe — список аргументов без оболочки |
| A04 Insecure Design | pass с замечаниями | журнал `intent → sent → restoring`, возврат в `finally`, стоп при сдвиге защищённого блока. Замечания по полноте возврата — review.md W-2, W-3 |
| A05 Security Misconfiguration | n/a | конфигурации сервисов в фиче нет |
| A06 Vulnerable and Outdated Components | не проверено локально | `numpy>=2`, `lxml>=5` — pip-audit в CI (задание `dependencies`); ffmpeg — вручную |
| A07 Identification and Authentication Failures | pass | см. «Пароль не повторяется после отказа» |
| A08 Software and Data Integrity Failures | pass | журнал атомарно с `fsync` (`write_journal.py:82-104`); XML из журнала — `defusedxml` + сверка защищённых блоков + `check_onvif` (`imaging.py:446-465`) |
| A09 Security Logging and Monitoring Failures | pass | обмен ONVIF и RTSP в доказательствах, текст хода вычищается |
| A10 Server-Side Request Forgery | pass | адрес RTSP из `GetStreamUri` переписывается на адрес камеры из стенда (`to_stand_address`, `frames.py:164`); адреса служб — `service_urls` стенда |

## Враждебный ввод (таблица спеки)

| Ввод | Результат | Где |
|---|---|---|
| огромное разрешение | частично | ограничены высота после сжатия до 1280 px (`frames.py:252`) и размер кадра PPM; верхней границы 8192 × 8192 по ffprobe из спеки нет — S-2 |
| бесконечный или плотный поток | pass | `Sampler(limit=32 МБ)`, `MAX_FRAMES=300`, срок `keyframe_wait_s + clip_s` |
| поток, ломающий декодер | pass | отдельный процесс, вход — временный файл, `DECODE_TIMEOUT_S` |
| диапазон: не число, NaN, ±∞, \|x\| > 10⁶, `min > max` | pass | `_number`, `_range` (`imaging.py:182-187`, `563-578`); тест `test_broken_ranges_are_never_written` |
| токен источника > 64 символов | частично | экранируется, но предел 64 → «пропущена» из спеки не сделан — S-3 |
| ответ SOAP > 64 КБ | pass | `MAX_SETTINGS_XML` (`imaging.py:319`) |
| текст камеры в HTML/PDF | pass | автоэкранирование; `src` — пути программы |

## Находки

| # | Уровень | Происхождение | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|---|
| S-1 | warning | новое | `checks/image/engine.py:446-448` | `_restore_all` ловит только `CameraUnreachable`, `OnvifFault`, `ImageWriteRefused`. Камера, которая вернула в `GetImagingSettings` элемент с именем запрещённой операции (`SetUser`), даёт `TabooViolation` на каждом возврате; в `finally` исключение уносит все строки проверки, `run_check` (`core/checks.py:269`) оставляет одну «ошибку стенда», а камера остаётся с изменёнными полями (запись одним полем проходит). Табу не нарушено (запрос не ушёл), но возврат AD-3 и строки протокола потеряны | ловить в `_restore_all` и `TabooViolation` (и любой `Exception`) с итогом в строке возврата «не прошла» |
| S-2 | note | новое | `checks/image/frames.py:246-253` | спека («Враждебный ввод»): по ffprobe больше 8192 × 8192 → `FrameTooLarge`. Сейчас ограничена только высота после сжатия; поток 16384 × 9216 декодируется целиком (сотни МБ на кадр в процессе ffmpeg) | проверка `width > 8192 or height > 8192` до запуска ffmpeg |
| S-3 | note | новое | `drivers/camera/imaging.py:255` | токен длиннее 64 символов (ReferenceToken) не ведёт к «пропущена», как в спеке. Риск низкий: токен экранируется | проверка длины в `Engine.run` |
| S-4 | note | новое | `checks/image/engine.py:543` | в журнал как `restore["token"]` пишется `writer.source` — XML-обёртка `<timg:VideoSourceToken>…</timg:VideoSourceToken>`, а не токен; при возврате по журналу берётся токен текущего прогона. Безопасности не вредит, но поле журнала не то, что называет | писать сам токен |

## Круг 2 (ce867a6, фикс B-1)

Дифф `cf21c58..HEAD`: `checks/image/engine.py`, `tests/checks/test_image_effect.py`, `progress.md`. Новых запросов к камере, клиентов, сокетов, зависимостей, хуков и настроек `.claude/` нет. Новые ветки разбирают уже полученные снимки (`Capture.stable`, `Capture.lumas`) и читают только пороги стенда (`Timings.of`). Табу AD-7 и запрет записи без галочки не затронуты: `engine.py:246-248`. Секретов в диффе нет. Причина строки состоит из чисел и порогов, текста камеры в ней нет. Сканеры не перезапускались: Snyk не установлен; pip-audit и agentshield не нужны, потому что зависимости, хуки и `.claude/` не менялись. Номера строк `engine.py` в таблицах выше — по состоянию до ce867a6 (сдвиг +21…+57). Вердикт — **pass**, без изменений.
