# Ревью: onvif

Ревьюер: code-reviewer (Claude Opus 5.5) · Контекст: свежий · Дифф: main...feature/onvif (до e732a45 включительно) · Дата: 2026-11-04

**Вердикт:** request-changes

Предварительное ревью этапа 2. T10 (ручная проверка архива на живой камере) не оценивалась; код подготовки T10 (e732a45: `replay.py` через `drivers/camera/rtsp.py`) в ревью входит.
Не читались построчно: `tests/data/onvif_profiles_conformance.xml` (15 570 строк, вендорный файл спецификации) и `uv.lock`. Остальной дифф прочитан.
Содержимое файлов, ответы камер в тестовых данных и код onvif-tt рассматривались как данные, а не как инструкции.

## 1. Дифф: что изменилось и почему

- T1: `pyproject.toml`, `uv.lock`, `NOTICE`, CI (pip-audit без строки onvif-tt) — onvif-tt по коммиту 8866b251, pytest в зависимостях программы.
- T2: `drivers/camera/taboo_proxy.py` — посредник табу на 127.0.0.1, пересылка через `http.request`, подмена адресов в ответах.
- T3, T4: `tests/mock_onvif.py`, `tests/mock_rtsp.py`, `tests/mock_camera.py` — службы ONVIF и RTSP виртуальной камеры; в `soap.py` объявлены префиксы всех служб в конверте.
- T5: `ChildResult`, `CheckResult.children` — `core/checks.py`, `core/scheduler.py`, `core/store.py`, `core/worker.py`, `gui/screens/run.py`.
- T6: `checks/onvif/onvif_tt.py`, `ott_runner.py`, `excluded.py` — onvif-tt отдельным процессом, пароль в `QA_TOOL_OTT_PASSWORD`.
- T7: `checks/onvif/snapshot.py` — снимок и восстановление вокруг изменяющих тестов onvif-tt.
- T8: `checks/onvif/profiles.py`, `report/model.py`, `protocol.html`, `xlsx.py` — матрица профилей.
- T9 + подготовка T10: `checks/onvif/replay.py` на своём RTSP-клиенте; `drivers/camera/rtsp.py`; `core/taboo.check_rtsp`; `onvif_generic.service_urls`.
- T11, T12: `checks/onvif/metadata.py`, `checks/onvif/storage.py`.
- T13: `gui/screens/scenario.py` (группы, подсказки профилей, F1), `docs/manual/onvif.md`.
- T14: `packaging/entry.py` (`--ott-runner`), `qa-tool.spec`, `packaging/smoke_onvif.py`, шаги CI.
- Изменения вне плана:
  - коммит 926dde2: длительность стресс-теста — выпадающий список (`gui/screens/scenario.py`, `STRESS_CHOICES`) — решение директора, к ONVIF не относится;
  - `.githooks/pre-push`: исполнитель `qa-limited` заменён на `qa-test`; `AGENTS.md`: описание песочницы VPS; `docs/rules/never.md`: две записи «Ошибки модели»;
  - `tools.tool_path` читает `QA_TOOL_FFMPEG_DIR`; `http.py` приглушает журнал httpx до WARNING; в `pyproject.toml` для `tests/**` отключено правило E501.
  - Отступления, записанные в progress.md: в T2 сокет посредника не добавлен в ALLOWED теста архитектуры (план требовал); в T8 список функций составлен вручную вместо разбора файла; в T12 объём, свободное место и форматирование отложены до этапа 3 (записано в спеке).

## 2. Соответствие спеке

| Сценарий | Реализован | Тест | Замечание |
|---|---|---|---|
| S1 onvif-tt по любой камере | да | tests/checks/test_onvif_tt.py::test_onvif_tt_runs_through_proxy_one_row_per_test; tests/core/test_scheduler.py::test_composite_check_writes_child_rows_and_tasks | |
| S2 Табу на onvif-tt | частично | tests/drivers/test_taboo_proxy.py::test_forbidden_operation_never_reaches_camera; test_onvif_tt.py::test_taboo_stop_and_xfail_mapping | XAddr со схемой в верхнем регистре (`HTTP://`) не подменяется, и onvif-tt идёт к камере мимо посредника (находка 2). RTSP onvif-tt (ffprobe) идёт мимо `check_rtsp` и подмены хоста (находка 1). Сквозного теста «onvif-tt отправил запрещённое → строка "ошибка стенда" в протоколе» нет |
| S3 Изменяющие и разрушающие по галочкам | да | test_onvif_tt.py::test_reboot_only_with_both_flags, test_writes_snapshot_and_restore_row, test_restore_happens_even_when_run_hangs_and_failure_is_visible, test_no_snapshot_no_writes, test_write_skip_reason_in_russian; tests/checks/test_snapshot.py | Изменяющие тесты doorcontrol (`requires_writes`) в снимок не входят (находка 12) |
| S4 Матрица профилей | частично | tests/checks/test_profiles.py | Объявленный профиль, у которого не проверена ни одна функция, получает «прошла» (находка 4) |
| S5 Выдача архива | частично | tests/checks/test_replay.py::test_archive_found_and_played, test_camera_without_require_support_gets_retry_without_header, test_foreign_replay_host_is_replaced_by_stand_address | Условие «время в потоке соответствует записи» не проверяется (находка 5). На живой камере — T10 |
| S5a Нет записей | частично | test_replay.py::test_no_records_without_writes_is_skipped, test_no_records_with_writes_records_plays_and_restores_job | Нет сверки после возврата заданий записи; неудачный возврат не виден в протоколе (находка 3). Нет теста на «не удалось включить запись» |
| S6 Метаданные Profile M | да | tests/checks/test_metadata.py | |
| S7 SD-карта | да (с уточнением T12 в спеке) | tests/checks/test_storage.py | Объём, место и форматирование переданы в этап 3 — записано в спеке |
| S8 onvif-tt недоступен или упал | да | test_onvif_tt.py::test_timeout_and_missing_report_are_stand_errors | Нет тестов на битый JSON отчёта и на ImportError onvif_tt в `titles()` |

### Непокрытые сценарии и случаи — для test-writer

1. S2: сквозной тест — onvif-tt (или его заглушка через посредника) получает `ter:QAToolTaboo`, строка протокола — «ошибка стенда: запрещённая операция не отправлена» с названием операции.
2. S2: ответ GetServices/GetCapabilities с `HTTP://<камера>/…` и `Http://<чужой>/…` — после `rewrite` адресов камеры и чужих хостов не остаётся.
3. Пароль: onvif-tt не запускает ffprobe с `user:password@` в аргументах (тесты `requires_ffprobe` исключены или ffprobe недоступен запускателю); RTSP onvif-tt не идёт на чужой хост из GetStreamUri.
4. S4: камера объявляет S, строки onvif-tt отсутствуют или все «ошибка стенда» — `RUV-PROFILE-S` не «прошла».
5. S5: архив, у которого время кадров (расширение ONVIF) вне `[EarliestRecording, LatestRecording]`, или живой поток вместо архива — «не прошла».
6. S5a: `SetRecordingJobMode Active` отказан — «не прошла», режимы вернулись; возврат режима первого задания упал — остальные всё равно возвращены, в протоколе есть «камера осталась изменённой»; камера приняла Idle, но осталась Active — расхождение видно после сверки.
7. S8: отчёт onvif-tt — битый JSON → «ошибка стенда: отчёт onvif-tt не разобран»; onvif_tt не импортируется → «ошибка стенда», остальные проверки камеры идут.
8. RTSP-клиент: 401 с Basic; `Content-Length` огромный или отрицательный; обрыв посреди кадра `$` (меньше 3 байт заголовка); бесконечная строка заголовка.
9. Посредник: строка запроса без ведущего `/` (`GET @10.0.0.1/x`) — запрос не уходит на чужой хост; ответ в `charset=iso-8859-1` с байтами вне кодировки.
10. Снимок: ответ GetImagingSettings без `ImagingSettings` — изменяющие тесты пропущены, прогон onvif-tt только на чтение идёт.

## 3. Безопасность

Вердикт security-check.md: fail (1 blocker, 1 major).

## 4. Тесты и edge cases

- Полный прогон: `unset VIRTUAL_ENV; LD_LIBRARY_PATH=~/.cache/qa-tool-syslibs/root/usr/lib/x86_64-linux-gnu qa-test uv run pytest -q` → 270 passed за 138 с.
- `uv run ruff check .` — чисто; `uv run ruff format --check .` — 124 файла в порядке; `scripts/check_comments.py` — код 0.
- На машине ревью ffprobe нет в PATH, поэтому тесты onvif-tt с `requires_ffprobe` здесь пропускаются. В CI ffmpeg добавляется в `GITHUB_PATH`, и там они идут (это условие находки 1).
- Не покрыто: список в разделе 2. Кроме того, не проверены: прерывание рабочего процесса по тайм-ауту зависания во время `record_for_a_while` и onvif-tt с «изменяющими» (`terminate()` не выполняет `finally`, восстановления не будет); дубли ID среди дочерних строк (папки доказательств перезаписываются).

### Комментарии (docs/rules/comments.md)

Язык — русский, закомментированного кода нет, docstring у публичного API есть (ruff D1 и check_comments.py проходят). Комментарии объясняют «почему»: тайм-ауты PullMessages, 551, `Require`, MD5 по RFC 2617, порядок PLAY/RTP. Устаревшие:
- `docs/rules/architecture.md` AD-7 по-прежнему называет сокеты «только `probe.py` и `discovery.py`», а тест архитектуры уже разрешает `rtsp.py`. Правило правит человек на ретро — вынести вопрос.
- `snapshot.py:3` — «изменяющие тесты закреплённой onvif-tt меняют …» — список неполный: нет doorcontrol (находка 12).

## Находки

| # | Уровень | Происхождение | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|---|
| 1 | blocker | новое | src/qa_tool/checks/onvif/ott_runner.py:370-382; src/qa_tool/drivers/camera/taboo_proxy.py:12; onvif-tt `cases/media.py:245-260` (8866b25) | Тесты onvif-tt с меткой `requires_ffprobe` (LOCAL-MEDIA-S-RTSP-LIVE, LOCAL-MEDIA-S-RESOLUTION/CODEC/FRAME-RATE/PTS-*, LOCAL-RTSS-*, LOCAL-MEDIA2-RTSP-LIVE) запускают `ffprobe … rtsp://<user>:<пароль>@<хост>/…`. Сценарий: на компьютере стенда ffprobe есть в PATH (Linux с пакетом ffmpeg; CI — да, через `GITHUB_PATH`). Тогда на каждый такой тест у каждой камеры пароль виден в списке процессов до 15 с. Это ровно то, от чего спека закрылась решением «пароль — переменной окружения». Хуже того: RTSP посредник не подменяет, и хост берётся из GetStreamUri как есть. Если камера вернула чужой адрес (NAT или недоверенная прошивка), ffprobe отправит учётные данные этому хосту; при Basic-вызове — открытым текстом. Это нарушает security.md («только адреса стенда») и п. 2 задания | В `Excluder` (ott_runner) убирать тесты с меткой `requires_ffprobe` и добавить их в `excluded.py` с причиной. Альтернатива — запускать onvif-tt с PATH без ffprobe (onvif-tt сам пропустит их: «ffprobe not installed»). Видеопоток подтверждать своими RUV-проверками через `rtsp.py`. В `profiles.py` для «Видеопоток RTSP» заменить эти ID на свои. Тест: в аргументах дочерних процессов нет пароля |
| 2 | major | новое | src/qa_tool/drivers/camera/taboo_proxy.py:205-207 | `_HTTP_URL` различает регистр схемы: `HTTP://192.168.1.10/onvif/media` в XAddr не подменяется вообще: ни на посредника, ни на `/_foreign` (проверено вызовом `rewrite`). requests и zeep схему в верхнем регистре принимают, поэтому onvif-tt пойдёт к службе камеры напрямую, мимо табу. С чужим хостом — к нему, с WS-Security. Закреплённая onvif-tt запрещённых операций не шлёт, так что вреда сейчас нет. Но гарантия AD-7 «ни onvif-tt, ни будущий модуль не обойдут» не держится | Добавить флаг `re.IGNORECASE` в `_HTTP_URL` и сравнивать схему через `scheme.lower()` в `to_proxy`. Тест на `HTTP://` и `Http://` |
| 3 | major | новое | src/qa_tool/checks/onvif/replay.py:91-118 | `record_for_a_while` возвращает режимы заданий записи без сверки (never.md: «восстановление и сверка», AD-3). Сценарий 1: возврат режима первого задания упал по тайм-ауту — цикл в `finally` прерывается, остальные задания остаются Active. В протоколе тогда только «не прошла: ONVIF: …», без «камера осталась изменённой». Сценарий 2: прошивка ответила OK на Idle, но пишет дальше — никто не узнает | Возвращать каждое задание в своём `try`. После возврата читать `GetRecordingJobs` и сравнивать с исходными режимами. Расхождение — отдельная строка или причина «камера осталась изменённой: задание X в режиме Active» со статусом «ошибка стенда», как `RUV-RESTORE-1` |
| 4 | major | новое | src/qa_tool/checks/onvif/profiles.py:181, 269-277 | Объявленный профиль без единой проверенной функции получает «прошла» («работают; не проверено функций: N»). Строки «ошибка стенда» `judge` считает «проверки пропущены», то есть UNCHECKED. Сценарий: onvif-tt упал (S8) или ONVIF-TT снят в сценарии — `RUV-PROFILE-S` «прошла» без доказательств работы функций. Это нарушает never.md («не засчитывать прошла без доказательства») и S4 | Если ни одна функция объявленного профиля не в WORKS — итог «пропущена» или «ошибка стенда» с причиной «функции профиля не проверены». Строки STAND_ERROR учитывать отдельно («не проверена: ошибка стенда») |
| 5 | major | новое | src/qa_tool/checks/onvif/replay.py:336-358 | S5: «прошла — кадры идут, время в потоке соответствует записи». Время кадров архива (`stats["archive"]`) только пишется в `actual` и ни с чем не сверяется. Сценарий: GetReplayUri отдаёт живой поток или архив с другого места — кадры и ключевые есть, итог «прошла». Это ровно тот дефект, ради которого существует блокерная `RUV-G-REPLAY-1` | Если есть время из расширения ONVIF — проверять, что оно попадает в `[start, end]` записи (с допуском из конфигурации), иначе «не прошла». Если расширения нет — писать в `actual`, что время не сверено. Порог — в конфигурацию |
| 6 | minor | новое | src/qa_tool/drivers/camera/taboo_proxy.py:104-112 | `camera_url` не требует ведущего `/`. Строка запроса `GET @10.9.9.9/x` даёт `http://<камера>:80@10.9.9.9/x`, а httpx берёт хост 10.9.9.9 (проверено). Посредник на время модуля работает открытым ретранслятором для любого локального процесса (табу при этом действует). Через onvif-tt недостижимо: zeep всегда шлёт путь с `/` | Если `path` не начинается с `/` — отказ `QAToolForeignHost`. Плюс проверка `httpx.URL(url).host == camera_address` перед отправкой |
| 7 | minor | новое | src/qa_tool/drivers/camera/rtsp.py:197-206 | Ответ камеры не ограничен: `readline()` без предела, `read(Content-Length)` при огромном значении выделяет память, при отрицательном читает до закрытия. Спека: «размер ограничен». Обрыв посреди `$`-кадра (`head` короче 3 байт) даёт `struct.error`, а не `RtspError`: итог «ошибка стенда: сбой проверки» вместо «не прошла» | `readline(8192)`; ограничить `Content-Length` (например, 1 МБ) и отрицательные значения → `RtspError`; короткий `head` → `RtspError` |
| 8 | minor | новое | src/qa_tool/checks/onvif/replay.py:63, 70, 108, 117, 127; metadata.py:96, 321; snapshot.py:112, 152, 158, 184, 246, 252 | Токены и значения из ответов камеры (SearchToken, JobToken, Mode, RecordingToken, ProfileToken, TZ) вставляются в тело SOAP без XML-экранирования. Табу это не обходит: `check_onvif` смотрит готовое тело. Но камера может сломать или подменить содержимое наших запросов к ней самой | `xml.sax.saxutils.escape()` для каждого значения из ответа |
| 9 | minor | новое | src/qa_tool/checks/onvif/replay.py:57-64; snapshot.py:187; replay.py:322 | Неожиданный ответ камеры даёт исключения, которые не ловятся: нет SearchToken → IndexError, нет ImagingSettings → IndexError, `struct.error` из RTSP. Итог — «ошибка стенда: сбой проверки», а не «не прошла»: дефект камеры не станет задачей программистам. IndexError в `SnapshotTaker.take()` выходит из `onvif_tt` (ловятся только CameraUnreachable, OnvifFault, ValueError), и весь ONVIF-TT становится ошибкой стенда вместо прогона только на чтение | Ловить IndexError в `take()` по областям (в `skipped`); в replay отсутствие токена — `OnvifFault` или «не прошла» с причиной |
| 10 | minor | новое | src/qa_tool/drivers/camera/taboo_proxy.py:180-183 | `decode(charset, "replace")` → `encode(charset)`: при однобайтовой кодировке U+FFFD не кодируется, будет UnicodeEncodeError. Обработчик падает, onvif-tt получает обрыв, а не ответ. Заголовок `Location` в ответе не подменяется | `encode(charset, "replace")` или кодировать в UTF-8 с исправленным заголовком; подменять адреса и в `Location` |
| 11 | minor | новое | src/qa_tool/checks/onvif/onvif_tt.py:260; replay.py:284 | Спека: «тайм-аут модуля — из сценария». Сейчас он берётся только из `ctx.extra`, а его не задаёт ни окно, ни командная строка: фактически всегда 1800 с и 120 с. При большем значении в `extra` предел зависания `MODULE_TIMEOUT_S + 120` и `DEFAULT_RECORD_S + 180` не растёт. Рабочий процесс тогда снимается `terminate()` до `finally` — без восстановления настроек | Брать из сценария или конфигурации порогов. `timeout_s` проверки выводить из того же значения, или ограничивать значение сверху |
| 12 | minor | новое | src/qa_tool/checks/onvif/snapshot.py:1-7 | Docstring говорит, что снимок покрывает всё, что меняют `requires_writes`-тесты закреплённой onvif-tt. Но doorcontrol (`cases/doorcontrol.py:222-228`, DOORCONTROL-3-1-19..27) тоже `requires_writes` и в снимок не входит. Для IP-камер редкость: служба doorcontrol бывает у устройств Profile D | Исключить эти тесты в `excluded.py` с причиной или дописать в docstring, почему они не восстанавливаются |
| 13 | minor | новое | tests/test_architecture.py:12-34; docs/rules/architecture.md AD-7 | План T2 требовал внести сокет посредника в ALLOWED; вместо этого в `NETWORK_MODULES` нет `http.server`, `socketserver` и `asyncio`, так что тест такие сокеты не видит вообще. AD-7 не упоминает `rtsp.py` и `taboo_proxy.py` | Добавить `http.server` и `socketserver` в `NETWORK_MODULES`, `taboo_proxy.py` — в ALLOWED. Предложить человеку правку AD-7 на ретро |
| 14 | minor | новое | src/qa_tool/checks/onvif/profiles.py:192 | Профиль ищется точным совпадением `onvif://www.onvif.org/Profile/Streaming`; варианты с `/` в конце или другим регистром не распознаются, и профиль ошибочно «не объявлен» | Сравнивать без учёта регистра и завершающего `/` |
| 15 | minor | новое | src/qa_tool/drivers/camera/onvif_generic.py:55-56 | Камера за NAT отдаёт XAddr со своим внутренним адресом; `service_urls` такие службы отбрасывает. Тогда RUV-G-REPLAY-1 «пропущена: камера не объявляет поиск…» — причина вводит в заблуждение. Для RTSP в той же ситуации адрес подменяется адресом стенда (`to_stand_address`) — поведение разное | В причину пропуска добавить «службы на чужом хосте отброшены: …». Либо по аналогии с RTSP подставлять адрес стенда — тогда правило в security.md уточнить с человеком |

### Старый долг (Гейт 3 не блокирует)

- `core/store.py:119, 151-153` — `redact` без известного пароля камеры: текст защищён только шаблонами. Для дочерних строк onvif-tt (сторонний вывод) это важнее, чем раньше, но сама схема старая.
- `drivers/camera/soap.py` и `http.request` — размер ответа камеры не ограничен (httpx читает всё в память).
- `core/scheduler.py:323, 352` — снятие зависшего рабочего процесса через `terminate()` не выполняет `finally` проверок; с появлением изменяющих проверок это риск AD-3.
