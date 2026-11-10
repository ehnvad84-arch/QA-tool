# Security-check: api-discovery

**Вердикт:** pass

Ревьюер: независимый ревьюер, Claude Opus 5.5 · Дифф: `git diff 1926aa6...HEAD` (33 коммита, 96 файлов; база — `feature/codec-quality`, из которой выросла ветка, см. review.md) · Дата: 2026-11-10

Проверяются только фреймворки, отмеченные применимыми в docs/rules/security.md: OWASP Top 10. OWASP LLM Top 10, OWASP Agentic AI Threats, MITRE ATLAS и NIST AI RMF не применимы: в продукте нет LLM, агентов и ML-компонентов (спека, «Безопасность»: подбор шкалы — прямая по трём точкам).

Блокирующих находок по безопасности нет. Пункты minor ниже — для следующего круга. Блокер функциональный — в review.md.

## Базовый минимум

- [x] В диффе и истории ветки нет секретов: в тестах только выдуманные пароли (`test-secret-1` … `test-secret-17`, `wrong-9`). Проверено поиском по `git log -p 1926aa6..HEAD -- tests/ packaging/` и по `git diff`. Новых файлов в `tests/data/` нет, ответы живых камер в репозиторий не скопированы: они лежат в `~/qa-trial-*`, вне git. В журнале записано, что настоящий пароль в тест T2 попал, но коммит был заблокирован, и в истории его нет. Поиск это подтверждает.
- [x] `.env` не в индексе. `.gitignore`, `.cursorignore` и `.claude/settings.json` в этом диффе не менялись.
- [x] Доступа к продакшну нет: прод-ключей, прод-адресов и команд деплоя в диффе нет. Адреса камер стенда упоминаются только в `progress.md` и спеке, сокращённо (`.111`, `.108`), без паролей.
- [x] Права минимальные. Новых сервисов и токенов нет. Вход в родной API идёт логином ONVIF из `keyring`, одна попытка (`HttpLogin`). Запись родным API — только возврат параметра картинки к значению M0 этого прогона.
- [x] Сканеры запущены, результат ниже.

## AD-7 (табу) — проверено по каждому новому выходу к камере

| Путь | Результат | Где |
|---|---|---|
| Чтение карты: сборщики → `NativeReader._get` → `taboo.check_native_read` → `http.request` (`check_http`, `check_onvif`, `MulticastGuard`, `check_web_request`) | pass | `drivers/camera/native.py:210-229`, `core/taboo.py:751-775` |
| Шаблоны сборщика: только GET, путь и запрос по белому списку символов, без хоста, `..`, `//` и переводов строк | pass | `native.py:231-241`; тест `test_dangerous_templates_never_reach_camera` |
| Опасные GET (Dahua `magicBox.cgi?action=reboot`, `setConfig`, выгрузка конфигурации, `factoryReset`, `upgrade`) | pass | `taboo.check_native_read`; `tests/core/test_taboo_native.py` (15 адресов) |
| «Рувер»: `ruver_fetch` пропускает только операции чтения из карты, `ruver_send` — только операции записи из карты; галочку проверяет сам `RuverClient` | pass | `ruver/collector.py:296-339` |
| Повтор с заголовком входа: табу проверяется заново, тело то же | pass | `http.py:181-200` (`_check` перед каждым `_send`) |
| Возврат S5 родным API: запрос строит сборщик только для картинки (Hikvision `RESTORABLE`, Dahua `IMAGE_TABLES` и ключ `^[A-Za-z0-9_.\[\]]{1,128}$`, «Рувер» — операция записи того же адреса). Отправка — `http.request` с табу. Журнал записывается до отправки | pass | `checks/api/restore.py`, `hikvision/collector.py:255-280`, `dahua/collector.py:165-180`, `discovery.py:173-192` |
| Автопоиск пишет только через ONVIF `ImagingWriter` и `EncoderWriter`, только по каталогу «S4 пишет» (`SearchRefused` до записи) | pass | `checks/api/search.py:400-402`; тест `test_search_refuses_settings_it_must_not_write` |
| Трафик веб-обхода: только пассивная запись того, что страница уже отправила; своих запросов нет | pass | `browser.py:_note_traffic`, `drivers/camera/traffic.py` |
| `tests/test_architecture.py` — новых исключений нет: сеть импортируют только `http.py`, `browser.py`, `probe.py`, `discovery.py`, `rtsp.py`, `taboo_proxy.py` | pass | `ALLOWED` в этом диффе не менялся; поиск `import httpx|socket|playwright` по `src/` ничего нового не нашёл |
| Адреса из ответов камеры, трафика и библиотеки не открываются; переадресации не выполняются | pass | `NativeReader.url` строит адрес только из `camera.address:port`; в `http._send` переадресаций нет (`httpx.Client` по умолчанию им не следует) |

## Секреты в артефактах

| Артефакт | Результат | Где |
|---|---|---|
| `api-map.json`, библиотека `map-*.json`, документ «Карта API» | pass, с замечанием S-2 | Значения секретов и чувствительных разделов отбрасываются в `Section.add_param` (`core/api_map.py:241-285`), при записи и при чтении. Тесты: поиск по байтам (`test_api_map.py`, `test_hikvision_map_and_compare`) |
| `exchanges.txt` | pass, с замечанием S-3 | Тело раздела, в котором есть хотя бы один несекретный и нечувствительный параметр, пишется только при отсутствии чувствительных; дальше `redact` и известный пароль (`discovery.py:111-117`, `native.py:344-351`) |
| Протокол (HTML/PDF/Excel) | pass | Jinja2 `autoescape=True` (`report/html.py:9`). В `_api.html` и `api_map.html` нет `|safe`. `href` строятся программой (`api_map_<место>.html`, `protocol_<место>.html`, `#s<n>`). В Excel строки с `data_type = "s"`, формулы из ответа камеры не исполняются. Тест `test_html_escapes_camera_text_and_includes_appendix` |
| Имена файлов и папок | pass | Имена доказательств — константы. Библиотека: `safe_name` + 8 знаков sha256, `resolve()` внутри корня, ссылки не читаются (`checks/api/library.py:40-71`); тест `test_folder_names_are_safe_and_distinct`, `test_hostile_files_are_skipped_with_reason` |
| `HttpLogin.__repr__` | pass | `password` и `on_refused` — `field(repr=False)` (`http.py:63-79`) |

## Сканеры

| Инструмент | Команда | Результат |
|---|---|---|
| pip-audit (заменил Snyk решением директора 2026-11-03, security.md п. 5) | `uv run pip-audit` | не запускался: локально не установлен, запуск через сеть в этой сессии не разрешён. Зависимости в диффе не менялись (`pyproject.toml`, `uv.lock` — без изменений). Проверит CI (задание `dependencies`) после push человеком |
| Snyk — зависимости (SCA) | `snyk test` | не запускался: в проекте заменён на pip-audit, ключа Snyk нет |
| Snyk — код (SAST) | `snyk code test` | не запускался: та же причина |
| agentshield | `agentshield scan --path .` | n/a: хуки, MCP, `.claude/`, скиллы и плагины в диффе не менялись |
| Claude Code Security / `/security-review` | запускает главная сессия | не запускался ревьюером |

## OWASP Top 10 (2021)

| Пункт | Результат | Где |
|---|---|---|
| A01 Broken Access Control | pass | Только адреса стенда (`NativeReader.url`), без переадресаций и без адресов из ответов. Запись родным API — только возврат картинки при выполнении условий из спеки (`restore.py`) |
| A02 Cryptographic Failures | pass | Basic по HTTP не отправляется (`http._choose`, тест `test_basic_over_plain_http_is_not_sent`). Digest MD5 выбирает камера. Пароль хранится только в `keyring`. Замечание S-4 (MD5 вторым заголовком) касается доступности, а не утечки |
| A03 Injection | pass | XML через `defusedxml` с `forbid_dtd=True`. Запрос входа камеры проверяется (`digest.check_challenge`: ≤ 256, без кавычек, CR/LF и управляющих). Ключи CGI — по белому списку символов, значения URL-кодируются. В HTML — автоэкранирование, в Excel — текст |
| A04 Insecure Design | pass | Автопоиск пишет только через ONVIF. Данные библиотеки, трафика и `confirmed.json` не становятся адресами, телами и значениями запросов (`library.py`, `restore.py:_plan` берёт значение только из M0) |
| A05 Security Misconfiguration | pass | Новых настроек безопасности нет. Пороги `api.*` проверяются (`limits.bad_thresholds`) |
| A06 Vulnerable and Outdated Components | n/a в этом круге | Зависимости не менялись. pip-audit — в CI |
| A07 Identification and Authentication Failures | pass, с замечанием (review.md, W1) | Одна попытка входа: `LoginRefused` остаётся до конца прогона, признак отказа общий (`rtsp_refusal`). Тесты `test_wrong_password_one_attempt_and_shared_refusal`, `test_refused_login_is_one_attempt_per_run`. Правка веба `login.py:183-191` в этом диффе ослабляет признак отказа (W1 в review.md) |
| A08 Software and Data Integrity Failures | pass | Журнал и библиотека пишутся атомарно (`write_journal.write_atomic`). При чтении — схема, версия и пределы (`loads_map`, `loads_pairs`, `library._read`). Список `native` журнала прошлого прогона только показывается, не отправляется (`old_native_lines`) |
| A09 Security Logging and Monitoring Failures | pass, с замечанием S-3 | Обмены идут через `redact`, тела чувствительных разделов не пишутся |
| A10 SSRF | pass | Ссылки из ответов, трафика и библиотеки не открываются. Трафик чужих хостов не попадает в карту (`is_camera_url`) |

## Находки

Происхождение у всех — «новое» (внесено этим диффом).

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| S-1 | minor | `src/qa_tool/drivers/camera/browser.py:486-517`, `src/qa_tool/checks/web/walk.py:206` | Спека («Сборщики брендов», «Трафик») требует: «в `ctx.extra` кладётся уже очищенное». На деле туда кладутся сырые `TrafficEntry` с телами ответов GET до 256 КБ и полными адресами с параметрами. Очистка происходит позже, в `from_traffic`. Данные живут только в памяти процесса камеры и в файлы не пишутся, поэтому риск низкий. Но любой будущий потребитель `WEB_TRAFFIC_KEY` получит неочищенные данные | Очищать при записи: адрес — через `traffic._section_id`, тело — сразу превращать в листья, либо записать отклонение в спеку |
| S-2 | minor | `src/qa_tool/core/api_map.py:85-95` | Слова `session`, `sid`, `cookie`, `auth` (как имя ключа ответа) не считаются секретом. В карте по трафику (S2) поле ответа GET вида `{"sessionId": "…"}` попадёт значением в `api-map.json`, в библиотеку и в документ «Карта API». `redact` его не поймает: в файле ключ — `"значение"`. security.md: «cookie сессии вырезаются». Нужна проверка: живого случая не было — у Tiandy и Beward трафика не было, «Рувер» служебные адреса входа не читает | Добавить `session(id)?|sid|cookie` в `_SECRET_WORD` и тест в `test_traffic.py` |
| S-3 | minor | `src/qa_tool/drivers/camera/native.py:344-351` | Тело для `exchanges.txt` скрывается, только если в разобранном разделе есть чувствительный параметр. Строка, которую сборщик отбросил при разборе (ключ Dahua вне белого списка или длиннее 128, id длиннее 256), остаётся в сыром теле и при этом не классифицируется. Если в ней секрет с ключом, который не знает `redact`, он уйдёт в доказательства. Нужна проверка: на образцах .108 и .114 таких строк не видно | Скрывать тело, если `parsed.skipped > 0`, или прогонять сырое тело через `is_secret` по ключам |
| S-4 | minor | `src/qa_tool/drivers/camera/http.py:202-211` | Берётся только первый заголовок Digest. Если камера предлагает `algorithm=SHA-256` первым, а MD5 вторым (так рекомендует RFC 7616), получим `LoginUnsupported` и «ошибку стенда», хотя MD5 доступен. Пароль при этом не уходит, так что это вопрос доступности, а не утечки. Живые Hikvision и Dahua 10.11 вошли | Брать первый заголовок, прошедший `check_challenge`. Тест — как `tests/drivers/test_rtsp.py:523` |

## Круг 2 (после 36e555e)

**Вердикт:** pass. Дифф: `git show 36e555e` (`checks/api/search.py`, `search_run.py`, `tests/checks/test_api_search.py`). Блокер круга 2 (B2) функциональный, он в review.md.

| Пункт | Результат | Основание |
|---|---|---|
| Новые выходы к камере в обход `core/taboo.py` | pass | Новые импорты в `search.py` — только классы исключений (`ValueRejected`, `ImageWriteRefused`, `EncoderWriteRefused`, `CameraUnreachable`, `NativeUnreachable`, `TransportError`). Ожидание камеры — существующий `WritePass._wait_for_camera` → `core/worker.camera_alive` (TCP-порт адреса стенда, без данных). Запись и возврат — те же `ImagingWriter`, `EncoderWriter`, `NativeRestorer`. `tests/test_architecture.py` зелёный |
| Одна попытка входа, отказ не глотается | pass | `LoginRefused` и Fault отказа пароля ONVIF (`credentials_rejected`) не входят в `REJECTED`, `NOT_SENT` и `UNREACHABLE`. Это проверено прогоном вне репозитория: итог «вход в родной API отвергнут» |
| Секреты в новых текстах | pass | В `search.json` и строки `API-SEARCH-1` и `API-RESTORE-1` идут текст исключения драйвера и подкод Fault камеры. Пароля и заголовков входа в них нет. В HTML они автоэкранируются |
| Пароли в новых тестах | pass | Только `SECRET = "test-secret-11"` |
| Сканеры | без изменений | Зависимости не менялись. pip-audit — в CI. Snyk не запускался (заменён). agentshield — n/a: `.claude/`, хуки и MCP не менялись |
| OWASP A07 | pass, с замечанием S-5 | см. ниже |
| Остальные пункты OWASP Top 10 | pass, как в круге 1 | Фикс их не затрагивает |

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| S-5 | minor (нужна проверка: живая Hikvision или Dahua после перезагрузки) | `src/qa_tool/checks/api/search_run.py:299-301`; `src/qa_tool/drivers/camera/http.py:187-193` | Внесено 36e555e. После ожидания пропавшей камеры запросы родного API идут со старым запросом входа Digest. Перезагруженная камера, которая отвечает 401 на старый `nonce` без `stale=true`, будет записана как «пароль отвергнут» на весь прогон. Утечки и повторного неверного пароля нет; это ложный отказ, то есть вопрос доступности. Подробно — review.md, m11 | Сбрасывать `HttpLogin.challenge` и `count` после «камера вернулась» |

S-1 … S-4 открыты, фикс их не касается.
