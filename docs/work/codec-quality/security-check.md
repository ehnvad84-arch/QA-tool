# Security-check: codec-quality

**Вердикт:** pass <!-- fail, если ниже есть хотя бы один fail -->

Ревьюер: code-reviewer (свежий контекст) · Дифф: `origin/feature/image...HEAD` (62 файла, собственные изменения codec-quality), из `feature/image` — используемые `checks/write_pass.py`, `report/charts.py`, `checks/image/frames.py` · Дата: 2026-11-10

Проверяются только фреймворки, отмеченные применимыми в docs/rules/security.md: OWASP Top 10. OWASP LLM Top 10, OWASP Agentic AI Threats, MITRE ATLAS, NIST AI RMF — не отмечены (LLM и агентов в продукте нет; VMAF — готовая модель ffmpeg без обучения и пользовательского ввода).

## Базовый минимум

- [x] В диффе нет секретов: пароль камеры только из `keyring` через `ctx.password`; в аргументах ffmpeg только пути временных файлов (`quality.py:108`, `:161`), тест `test_ffmpeg_gets_only_files_and_writes_log_by_relative_path`; текст сбоя проверки — через `redact` (`engine.py:308`); обмен ONVIF/RTSP в доказательствах — `soap.exchanges` и `RtspFrameSource.exchanges` (без секретов по их контракту, этап 2–7). Адреса живых камер в `progress.md` — только последние октеты, паролей нет.
- [x] `.env` не в индексе; `.gitignore`, `.cursorignore`, `.claude/` в диффе не менялись.
- [x] Нет доступа к продакшну: команд деплоя и прод-ключей нет.
- [x] Новых сервисов, токенов и агентов нет.
- [x] Сканеры — ниже (pip-audit локально не запускался).

## Табу AD-7 (правило проекта, проверяется в каждом PR)

| Пункт | Результат | Где |
|---|---|---|
| Новый клиент к камере идёт через `core/taboo.py` | pass | `EncoderWriter` ходит только через `SoapClient` → `drivers/camera/http.py` (`check_http`, `check_onvif`, `MulticastGuard`); RTSP — только `RtspFrameSource`/`RtspClient` (`RTSP_READ_METHODS`); новых сокетов нет, `tests/test_architecture.py` без новых исключений |
| Операции кодировщика не в `FORBIDDEN_ONVIF` | pass | `Get/SetVideoEncoderConfiguration(s)`, `GetVideoEncoderConfigurationOptions`, `GetProfiles`, `GetStreamUri` |
| Мультикаст (T2, решение директора 08.11) | pass | `core/taboo.py:135-188` `MulticastGuard` в общей точке `http.py:83-92`: `Set*Configuration` с другим, не прочитанным или неразборчивым `Multicast` — `TabooViolation` (fail-closed); через посредника onvif-tt — `tests/drivers/test_taboo_proxy.py::test_multicast_change_from_onvif_tt_is_refused_by_proxy`. Вторая линия — белый список драйвера `encoder.py:578-588` (`Multicast` канонически как в снимке) |
| Разрешение, частота, GOP, Quality, SessionTimeout, Name не меняются | pass | `encoder.py:37-46` `KEPT` + `GovLength`, блок строится из снимка; `test_block_changing_whitelisted_field_is_never_sent` |
| XML из журнала прошлого прогона | pass | `encoder.py:603-637`: ≤ 64 КБ, defusedxml, корень и токен, вложенная запрещённая операция → `TabooViolation` (`test_restore_xml_with_nested_set_user_is_taboo`) |
| Запись только с галочкой и после журнала | pass | `engine.py:250-254`, журнал `intent` до записи `engine.py:446-450`; `test_without_checkbox_nothing_is_written` |
| Сеть, пароли, сброс — не трогаются | pass | в диффе таких операций нет |

## Сканеры

| Инструмент | Команда | Результат |
|---|---|---|
| pip-audit (замена Snyk, security.md п. 5) | `pip-audit` | не запускался локально: в окружении не установлен, запуск через `uvx` требует одобрения в ночной сессии. Зависимости в диффе не менялись (`pyproject.toml`, `uv.lock` без правок) — проверяет задание CI `dependencies` |
| Snyk — зависимости (SCA) | `snyk test` | не запускался: Snyk заменён на pip-audit (решение директора 2026-11-03), ключа нет |
| Snyk — код (SAST) | `snyk code test` | не запускался: то же |
| agentshield | `agentshield scan --path .` | не нужен: хуки, MCP, `.claude/`, скиллы и плагины в диффе не менялись |
| Claude Code Security / `/security-review` | запускает главная сессия | — |

## OWASP Top 10 (2021)

| Пункт | Результат | Где |
|---|---|---|
| A01 Broken Access Control | pass | `codec_history._inside` (`codec_history.py:46-50`): папка доказательств прошлого прогона после `resolve()` — внутри его папки; база прошлых прогонов `mode=ro` (`store.py:66-67`); `test_evidence_folder_outside_its_run_is_ignored` |
| A02 Cryptographic Failures | n/a | новой криптографии и хранения секретов нет |
| A03 Injection | pass | HTML: Jinja2 `autoescape=True` (`html.py:9`), в `_codec.html` и `codec_compare.html` нет `|safe`; имена профилей и паспорт от камеры — текстом. XML: токен — `escape()`, атрибуты — `quoteattr` (`encoder.py:373`, `:561`), `Encoding`/`Profile` — только из `Options` камеры. Excel: все строковые ячейки `data_type = "s"` (`xlsx.py:62-63`), формулы не создаются. ffmpeg: список аргументов без shell, граф фильтров без данных камеры (`quality.py:111-162`), лог по относительному пути. Имена файлов доказательств не строятся из ответа камеры |
| A04 Insecure Design | pass | табу в общей точке, fail-closed для мультикаста; журнал до записи; возврат в `finally`. Замечания по полноте возврата — в review.md (W4–W6), не уязвимость |
| A05 Security Misconfiguration | pass | ffmpeg с пониженным приоритетом и сроком (`quality.py:190-217`), RTSP 10 с, SOAP 10 с |
| A06 Vulnerable and Outdated Components | pass (с оговоркой) | новых зависимостей нет; версия ffmpeg сборки ведётся вручную (pip-audit её не видит) — как записано в спеке |
| A07 Identification and Authentication Failures | pass | отказ входа — без повтора (`refused_password`, `credentials_rejected` → `Abort`, `engine.py:227-229`, `:454-457`) |
| A08 Software and Data Integrity Failures | pass | журнал атомарный (`write_journal.save`); `measure.json` ≤ 1 МБ, версия 1, числа через `_finite`; лог VMAF ≤ 4 МБ, числа в диапазоне (`quality.py:220-268`) |
| A09 Security Logging and Monitoring Failures | pass | обмен ONVIF/RTSP — в доказательства строк (`engine.py:750-764`) |
| A10 SSRF | pass | адреса только со стенда: `service_urls`, `RtspFrameSource` приводит перечитанный `GetStreamUri` к адресу стенда (`frames.py:177` `to_stand_address`) |

## Враждебный ввод (таблица спеки)

| Ввод | Результат | Где |
|---|---|---|
| `BitrateRange` не число, NaN, ±∞, > 10⁶, `min > max` | pass | `encoder.py:165-171`, `:230-244`; `test_broken_bitrate_ranges` |
| чтение назад не то | pass | `encoder.py:657-664` → «камера не приняла значение» |
| токен > 64 символов | частично | отдельного отказа на токен > 64 нет: токен уходит через `escape()` и ограничен `MAX_CONFIG_XML`; инъекции нет — minor, см. review.md N8 |
| XML > 64 КБ | pass | `encoder.py:384-385`, `:615-616` |
| огромный кадр, плотный поток | pass | выборка ≤ 32 МБ и ≤ 300 кадров (`frames.py:49-50`), `FrameTooLarge`; ffmpeg — отдельный процесс со сроком |
| лог VMAF | pass | `quality.py:220-268`; `test_values_out_of_range_are_rejected`, `test_broken_or_huge_log_is_rejected` |
| прошлые прогоны | pass | см. A01 и A08 |

## Временные файлы

- pass: выборки — в `qa-codec-<место>-*` (`engine.py:929-932`), удаляются в `finally` `Engine.run` (`engine.py:220-224`) и уборкой при старте прогона (`tempfiles.py:16-17`, тест `test_runner_removes_browser_files_of_killed_process`); рабочая папка ffmpeg — `vmaf-*` внутри неё, удаляется после каждого сравнения (`engine.py:612-627`).
- minor (review.md N3): выборки прошлых кодировщиков лежат до конца проверки, а не только текущего, как в спеке.

## Находки

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| 1 | minor (новое) | `src/qa_tool/core/taboo.py:160` | `MulticastGuard` помнит мультикаст по `(хост, токен)` без типа конфигурации. У камер с одинаковыми токенами видео- и аудиокодировщика последний разобранный ответ перезапишет чужой мультикаст, и законная запись получит `TabooViolation`. Отказ безопасный (запрос не уходит), но даёт «ошибку стенда» | ключ `(хост, локальное имя элемента-конфигурации или операции, токен)` |
| 2 | minor (новое) | `src/qa_tool/checks/codec/engine.py:780-781`, `:810` | `TabooViolation` от `MulticastGuard` при возврате не ловится: выходит из `Engine.run`, строки и `CODEC-RESTORE-1` теряются, в протоколе этого прогона нет блокера (журнал остаётся — блокер будет в следующем прогоне). Подробнее — review.md W4 | ловить `TabooViolation` в `_restore_encoder` и `_restore_all` как отказ возврата |
