# Ревью: codec-quality

Ревьюер: code-reviewer (Claude, свежий контекст) · Контекст: свежий · Дифф: `origin/feature/image...HEAD` (62 файла, 30 коммитов — собственные изменения codec-quality); полный `origin/main...HEAD` — 221 файл, из него код `feature/image` смотрелся только там, где его использует или переносит codec-quality (`checks/write_pass.py`, `checks/image/engine.py`, `checks/image/frames.py`, `report/charts.py`) · HEAD 4bbd7c1 · Дата: 2026-11-10 · Круг: первый

**Вердикт:** request-changes — один blocker (B1): камера без постоянного битрейта получает ложное «не прошла» на каждой ступени. Полный прогон тестов не завершён (раздел 4): по нему одобрить нельзя до прогона главной сессией.

Остальной код `feature/image` (этап 7) этим ревью не одобряется: он ревьюится в своей ветке.

## 1. Дифф: что изменилось и почему

- `core/taboo.py` `MulticastGuard` + `drivers/camera/http.py` — табу на изменение мультикаста в `Set*Configuration` в общей точке выхода, в том числе для onvif-tt через посредника (T2).
- `checks/write_pass.py` — общий проход изменяющей проверки (журнал, журнал прошлого прогона, ожидание пропавшей камеры, строка возврата), вынесен из `checks/image/engine.py`; «Картинка» переведена на него (T3). Сверил построчно: смысл «Картинки» не изменился — порядок `settle_old`, исключения `refused` + `TabooViolation` + `ValueError`, закрытие журнала те же.
- `core/write_journal.py` — вид журнала `CODEC` (`pending-codec/`, `CODEC-WRITE-LOST`), `core/tempfiles.py` — префикс `qa-codec-` (T3, T6).
- `drivers/camera/encoder.py` — `EncoderWriter`: конфигурации, варианты, запись, чтение назад, возврат, белый список (T4).
- `checks/codec/ladder.py`, `quality.py`, `rules.py`, `engine.py`, `__init__.py` — ступени, VMAF/SSIM/PSNR, пороги и итоги, проход по кодировщикам, регистрация `CODEC-QUALITY-1` (T5, T7–T11).
- `checks/image/frames.py` — параметры `timings`, `sample_bytes`, `prefix`, `on_packet` (T6).
- `report/charts.py` `xy_chart`, `report/codec.py`, `codec_compare.py`, `codec_history.py`, `model.py`, `publish.py`, `xlsx.py`, шаблоны — приложение «Кодеки», Excel, сравнение кодеков, «эта прошивка / прошлая» (T14–T17).
- `gui/run_view.py`, `gui/screens/scenario.py`, `gui/screens/protocols.py` — «строк» для `CODEC-`, подписи галочки, строка «Сравнение кодеков» (T13, T16).
- `docs/manual/codec.md` и правки справки (T18); `packaging/smoke_onvif.py` `smoke_codec` (T19).
- Моки: `tests/mock_onvif.py` (кодировщики Media2/Media1, неисправности), `tests/mock_rtsp.py` (видео по кодеку и битрейту, H.265 по RFC 7798).
- Изменения вне плана:
  - `docs/work/trial-11-09/plan.md` — план живых испытаний 09.11 (документ, к коду не относится);
  - `docs/rules/never.md` — урок 09.11 о сверке по полному XML;
  - `packaging/smoke_web.py` — поиск папок прогона по `QA-*` (рядом появилась `pending-codec/`; объяснено в progress.md, T19);
  - `tests/core/test_taboo.py` — подмена `httpx.request` теперь возвращает ответ (нужно `MulticastGuard.remember`; объяснено в progress.md, T2);
  - `tests/gui/test_scenario_run.py::test_image_note_under_writes_checkbox_follows_it` — проверка ослаблена с «подпись скрыта» до «в подписи нет «Картинки»»: подпись теперь общая с «Кодеками». Обосновано, но см. N7.

## 2. Соответствие спеке

| Сценарий | Реализован | Тест | Замечание |
|---|---|---|---|
| S1 кривая качества профиля | частично | `tests/checks/test_codec_engine.py::test_healthy_encoder_passes_both_codecs_and_is_restored`, `::test_three_encoders_get_own_rows_and_all_are_restored`, `tests/checks/test_codec_fleet.py::test_eight_cameras_measure_codecs_with_image_in_sync_phase`, `tests/checks/test_codec_ladder.py`, `tests/report/test_codec_protocol.py` | B1: «при постоянном битрейте» не выполняется у камер без `ConstantBitRateSupported` и у Media1, а итог считается так, будто выполняется. Снимков центра 1:1 нет (W8, отклонение записано в progress.md). Сетка `< 720p` не как в спеке (W8) |
| S2 общий эталон и сравнение кодеков | да | `tests/checks/test_codec_rules.py::test_reference_is_the_stream_the_other_loses_more_to`, `tests/report/test_codec_protocol.py::test_better_codec_sentence_counts_common_steps` | снимки и ссылки ступеней второго кодека сдвинуты (W1) |
| S3 провал качества — задача программистам | да | `test_codec_engine.py::test_bitrate_off_fails_with_measured_value`, `::test_quality_not_growing_with_bitrate_fails`, `test_codec_rules.py::test_below_threshold_on_step_above_floor_fails`, `::test_quality_not_growing_beyond_spread_fails`, `::test_bitrate_off_fails_with_deviation_and_tolerance` | «снимки эталона и ступени» в шагах — со сдвигом у второго кодека (W1) |
| S4 камера не приняла настройку | частично | `test_codec_engine.py::test_camera_keeping_its_bitrate_did_not_accept_value`, `::test_fault_on_write_fails_row_and_nothing_is_left_changed`, `::test_undeclared_second_codec_is_skipped`, `tests/drivers/test_encoder.py::test_codec_that_does_not_fit_current_resolution_fps_gop_is_named` | отказ эталона текущего кодека (живой случай Tiandy) останавливает весь кодировщик: второй кодек — «ошибка стенда», а не «остальные продолжаются» (W3) |
| S5 без галочки | да | `test_codec_engine.py::test_without_checkbox_nothing_is_written`, `::test_state_row_has_current_frame_and_bitrate_without_checkbox`, `tests/gui/test_scenario_run.py::test_codec_note_follows_writes_checkbox_and_sits_in_its_group` | — |
| S6 возврат | частично | `test_codec_engine.py::test_healthy_…_is_restored`, `::test_write_left_by_previous_run_is_returned_with_checkbox`, `::test_write_left_by_previous_run_without_checkbox_is_a_blocker`, `tests/drivers/test_encoder.py::test_restore_*`, `tests/checks/test_write_pass.py` | дыры в журнале и строке возврата: W2, W4, W5, W6 |
| S7 сцена сдвинулась | да | `test_codec_engine.py::test_moved_scene_is_stand_error_not_encoder_defect`, `test_codec_rules.py::test_scene_changed_is_stand_error_even_with_bad_values` | тест движка подменяет `moved`; ветка «предел замера < `repeat_vmaf_min`» на движке не проверена (есть в `rules`). Без повтора эталона сцена не проверяется (N6) |
| S8 камера пропала | частично | `test_codec_engine.py::test_camera_lost_midway_keeps_rows_and_journal` | проверен только исход «не вернулась»; «вернулась за `reconnect_wait_s` → возврат и сверка» тестом не покрыт |
| S9 сравнение камер и прошивок | да | `test_codec_fleet.py` (HTML/PDF/Excel сравнения), `tests/report/test_codec_history.py`, `tests/gui/test_scenario_run.py::test_protocols_screen_lists_codec_comparison` | плашка «изменяющие запрещены» отчёта сравнения тестом не покрыта |

### Ждёт живых камер (T20, не блокер ревью)

- Пороги и сетки `codec.*` (`repeat_vmaf_min` 90 на грани в помещении — Tiandy 84,8), время фазы на 4K (≈ 20 мин с «Картинкой»), перезапуск потока после смены кодека.
- Решения директора по находкам 09.11: уличные камеры вне испытаний качества; при «камера не приняла» с постоянным битрейтом пробовать переменный (связано с B1 и W3).
- Проверка исправления B1 на живой камере без `ConstantBitRateSupported` или только с Media1.

### Непокрытые сценарии и случаи — для test-writer

1. Камера без `ConstantBitRateSupported` (mock `encoder_cbr=False`) и камера только с Media1 в режиме переменного битрейта, поток на неподвижной сцене заметно ниже потолка → строки не должны быть «битрейт не соответствует» (B1). Нужна неисправность mock RTSP «переменный битрейт недобирает потолок»: сейчас mock всегда добивает поток до заданного.
2. Второй кодек с максимумом выше верха сетки (1080p, максимум 16384): ссылки Excel `sN.jpg` и «худшая ступень» листа указывают на снимок той ступени, чей битрейт в подписи (W1).
3. S8: камера пропала посреди ступени и вернулась за `image.reconnect_wait_s` → кодировщик возвращён и сверен, `CODEC-RESTORE-1` «прошла», журнал закрыт.
4. Журнал прошлого прогона на стадии `restoring` (процесс снят во время возврата) → следующий прогон возвращает, а не пишет «поменял кто-то другой» (W6).
5. Журнал прошлого прогона + отказ `GetVideoEncoderConfigurationOptions` в этом прогоне → строка `CODEC-WRITE-LOST` не теряется (W2).
6. Возврат кодировщика отвергнут сторожем мультикаста (`TabooViolation`) → строки и `CODEC-RESTORE-1` «не прошла» в этом же протоколе (W4).
7. Два кодировщика: возврат первого не сверился, второй записан, процесс снят → журнал помнит оба (W5).
8. Отказ записи эталона текущего кодека → итог строки второго кодека (W3, после решения).
9. S7 на движке: предел замера ниже `codec.repeat_vmaf_min` без подмены `moved`.
10. S9: отчёт сравнения без галочки «изменяющие» — плашка и таблица текущих настроек.
11. Токен кодировщика длиннее 64 символов → «пропущена» (N8).
12. Пометки строки: «потолок переменного битрейта» и «только Main10» видны в протоколе (B1, N9).

## 3. Безопасность

Вердикт security-check.md: pass. Табу AD-7 соблюдено: все запросы кодировщика — через `SoapClient` → `http.py` (`check_http`, `check_onvif`, `MulticastGuard`), RTSP — только `RtspFrameSource`; мультикаст закрыт и в общей точке (fail-closed), и белым списком драйвера. Ответы камеры в HTML экранируются (autoescape, без `|safe`), в Excel — текстом, в пути файлов не идут. pip-audit локально не запускался (зависимости не менялись). Две minor-находки — в security-check.md.

## 4. Тесты и edge cases

- Линтер: `uv run ruff check . && uv run ruff format --check .` → «All checks passed!», «262 files already formatted».
- Комментарии: `uv run python scripts/check_comments.py` → без замечаний.
- Полный прогон: `qa-test env LD_LIBRARY_PATH=$HOME/.cache/qa-tool-syslibs/root/usr/lib/x86_64-linux-gnu uv run pytest -q -p no:cacheprovider -rf` → **не завершён: недостаточно данных.** Собрано 1355 тестов; за ≈ 4 ч в песочнице `qa-test` прошли первые **575 — все «passed», 0 failed, 0 skipped** (в том числе все тесты движка кодеков на настоящем libvmaf: `tests/checks/test_codec_engine.py`, `test_codec_fleet.py`, `test_codec_ladder.py`, `test_codec_quality.py`, `test_codec_rules.py`, `test_codec_stream.py`, а также «Картинка» на `write_pass`). Дальше прогон упёрся в браузерные тесты `tests/checks/test_web_write.py` (2–3 мин на тест) и был снят по окончании ревью. Не дошли: `tests/checks/test_write_pass.py`, `tests/core/test_taboo_multicast.py`, `tests/core/test_taboo.py`, `tests/core/test_scheduler.py`, `tests/drivers/test_encoder.py`, `tests/drivers/test_taboo_proxy.py`, `tests/report/test_codec_*.py`, `tests/report/test_charts.py`, `tests/gui/*`, `tests/test_architecture.py`. Главной сессии: прогнать их (или весь набор хуком `pre-push`) до Гейта 3. По progress.md CI 42565115234 (T19) был зелёным на обеих ОС, но это не HEAD 4bbd7c1.
- Не покрыто: см. список для test-writer выше; плюс конкурентность — `MulticastGuard` общий на процесс камеры и пишется из посредника onvif-tt и движка (сейчас фазы не пересекаются).

## Находки

Уровни: blocker / warning / note (по заданию главной сессии; warning ≈ major, note ≈ minor). Происхождение у всех — «новое» (внесено codec-quality); старый долг — отдельным разделом.

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| B1 | blocker (новое) | `src/qa_tool/checks/codec/engine.py:564-570` (`Curve(..., constant=True, ...)`), `:464` (`shot.form` нигде не читается); `src/qa_tool/checks/codec/rules.py:207` | Итог ступени всегда считается как при постоянном битрейте, хотя драйвер пишет потолок переменного (`form = CEILING`, `encoder.py:535-538`) у Media1 и у Media2 без `ConstantBitRateSupported`. Сценарий сбоя: камера только с Media1 (или Media2 без флага) в режиме переменного битрейта, неподвижная сцена бокса — поток законно в 2–3 раза ниже потолка; `bitrate_problem` с `constant=True` проверяет и «ниже» → на каждой ступени «не прошла: битрейт не соответствует заданному (−56 % при допуске ±20 %)», в протоколе ложные задачи программистам. Подтверждено вызовом `verdict` на кривой 4096→1500, 2048→900 кбит/с: `constant=True` — «не прошла», `constant=False` — «прошла». Спека: «постоянный — в обе стороны, переменный — только потолок»; справка F1 (`docs/manual/codec.md:17`) обещает «ставится потолком переменного, с пометкой» — пометки в строке тоже нет. Тестов на этот путь нет: mock RTSP всегда добивает поток до заданного | `Curve.constant` брать из `shot.form` (`form == CONSTANT`; режим лучше хранить в `Point` и передавать в `_bitrate`); пометку «потолок переменного битрейта» — в причину строки и `measure.json`; тест движка с `encoder_cbr=False` и недобирающим потоком |
| W1 | warning (новое) | `src/qa_tool/report/codec.py:359-364` и `:461-467` против `src/qa_tool/checks/codec/engine.py:729-731` и `:576-579`, `:589-594` | Номер снимка `sN` движок даёт по списку ступеней кодека, а протокол — по `measure.json["ступени"]`, где у второго (не эталонного) кодека есть ещё верхняя точка (его максимум, `engine.py:579`), а у текущего — точка неудачного возврата (`:592-594`). Сценарий: 1080p, максимум камеры 16384 > верха сетки 8192 → у второго кодека «1 из 6» — 16384, а `s1.jpg` — снимок 8192; все ссылки Excel «Снимок ступени» сдвинуты на одну, «худшая ступень» листа берёт снимок другого битрейта под подписью с чужим VMAF (или пропускает, если `s6.jpg` нет). Итог строк верен, но доказательство в шагах S3 — нет. Mock-прогон тоже даёт сдвиг (максимум 8192, сетка sd 2048), тест ссылки не проверяет. Ссылка «Снимок эталона» у второго кодека ведёт на его собственный максимум, а не на эталон | писать в `measure.json` у каждой точки имя её снимка (`"снимок": "s3"` / `"ref"` / нет) и брать его в отчёте, а не номер по сортировке; ссылку эталона — из папки строки кодека-эталона |
| W2 | warning (новое) | `src/qa_tool/checks/codec/engine.py:234-244`; `src/qa_tool/checks/write_pass.py:166` | `settle_old` закрывает журнал прошлого прогона и кладёт строку `CODEC-WRITE-LOST` (блокер) в `self.rows`, но если затем `_plan` → `options()` падает `OnvifFault`/`CameraUnreachable`, проверка возвращает `stand_error` без строк. Сценарий: прошлый прогон оставил кодировщик изменённым, возврат не удался (блокер), а в этом прогоне `GetVideoEncoderConfigurationOptions` ответил Fault — блокер пропадает из протокола, журнал уже удалён, следующий прогон о нём не узнает | при ошибке после `settle_old` возвращать `stand_error` с `children=self.writes.rows`; либо `_plan` до `settle_old` |
| W3 | warning (новое) | `src/qa_tool/checks/codec/engine.py:411-413`, `:571-575` | Камера отвергла эталон текущего кодека (максимум при постоянном битрейте — живой случай Tiandy 16 Мбит/с 09.11) → `_measure` выходит, второй кодек не снимается, его строка — «ошибка стенда: нет кадров…; эталон не снят». Спека S4: «эта ступень или кодек — не прошла; остальные продолжаются». Ложного «прошла» нет, но дефект камеры уходит в «ошибку стенда», а второй кодек не проверяется | при отказе эталона `A` продолжать с `B` (эталон — `B_max`); «пробовать переменный» — решение директора (находка 3 T20) |
| W4 | warning (новое) | `src/qa_tool/checks/codec/engine.py:780-781`, `:810-811` | `_restore_encoder` и `_restore_all` ловят `OnvifFault`, `ValueRejected`, `EncoderWriteRefused`, `CameraUnreachable`, но не `TabooViolation` (сторож мультикаста, `http.py:84`) и не прочие сбои. Сценарий: камера сама сменила порт мультикаста после смены кодека → возврат снимка с прежним мультикастом не уходит (правильно), но исключение выходит из `Engine.run`, `run_check` (`core/checks.py:269-270`) даёт общий `stand_error`, все строки кодеков и `CODEC-RESTORE-1` теряются; блокера в протоколе этого прогона нет (журнал остаётся — его покажет следующий прогон) | ловить `TabooViolation` (и `Exception` с `redact` в `_restore_all`) как «возврат не выполнен: …» → `CODEC-RESTORE-1` «не прошла» |
| W5 | warning (новое) | `src/qa_tool/checks/codec/engine.py:446`, `:777`, `:785`; `src/qa_tool/core/write_journal.py` (один файл на адрес и порт) | Журнал хранит одну запись на камеру. Сценарий: возврат кодировщика 1 не сверился (`report.left`) или упал — журнал не закрыт; запись кодировщика 2 перезаписывает журнал своим токеном и XML; процесс снят планировщиком (срок, зависание) → следующий прогон вернёт только кодировщик 2, кодировщик 1 остаётся изменённым без блокера | не переходить к следующему кодировщику, пока журнал предыдущего не закрыт (кодировщики после — «ошибка стенда»), или хранить в `restore` список незакрытых кодировщиков |
| W6 | warning (новое) | `src/qa_tool/checks/codec/engine.py:777`, `:805` | Перед возвратом журнал пишется с `new = label(snapshot)` — то есть `new == old`. Если процесс снят во время возврата, следующий прогон (`write_pass.py:180-188`) видит «сейчас» = последняя ступень ≠ `new` и пишет блокер «его поменял кто-то другой» вместо возврата из журнала. «Картинка» на стадии `restoring` хранит последнее записанное значение (`_last_written`) — поведение разошлось | на стадии `restoring` писать в `new` последнее записанное значение (как «Картинка») |
| W7 | warning (новое) | `src/qa_tool/checks/codec/engine.py:486` | Ступени второго кодека снимаются по `plan.config.uri` — адресу, прочитанному до смены кодека; новый адрес, полученный `stream_uri` при съёмке эталона `B`, в план не попадает. Если камера меняет адрес с кодеком, каждая ступень `B` открывается со второй попытки после `restart_wait_s` (20 с × 5 ступеней), риск выйти за бюджет срока | после смены кодека брать для ступеней того же кодека адрес, полученный при смене |
| W8 | warning (новое) | `src/qa_tool/checks/codec/ladder.py:20-22`; `docs/work/codec-quality/progress.md` (T5) | Отклонения от утверждённой спеки: (а) сетка для `< 720p` — верх 2048, а спека даёт «≤ 720p — 4096/…/256»; progress.md (T5) утверждает «стартовые точки те же, что в спеке» — для субпотоков 640×360/704×576 это не так; (б) снимков центра 1:1 (`ref_c.jpg`, `sN_c.jpg`) нет — в листе 2 снимка вместо 4 (UI спеки, п. 4; отклонение записано в Handoff) | решение главной сессии: поправить спеку (Гейт 1) или код; отклонение (а) дописать в progress.md |
| N1 | note (новое) | `src/qa_tool/core/taboo.py:160` | Ключ памяти мультикаста `(хост, токен)` без типа конфигурации: при одинаковых токенах видео и аудио — ложный отказ (безопасный) | см. security-check.md, находка 1 |
| N2 | note (новое) | `src/qa_tool/drivers/camera/encoder.py:693-697` | Media1: GOP и профиль лежат в блоке `H264`, который в `compare` попадает в «прочие» (сведение, не блокер). Если камера сама сменит GOP Media1 при возврате, блокера не будет. После урока 09.11 (never.md, MaxGain) стоит сверять и его | для Media1 добавить `H264/GovLength` в точную сверку |
| N3 | note (новое) | `src/qa_tool/checks/codec/engine.py:528-530` | Выборки всех кодировщиков лежат во временной папке до конца проверки; спека — «только текущего кодировщика» (≤ 350 МБ). Проверка 500 МБ перед каждым кодировщиком смягчает | удалять выборки кодировщика после `_results` |
| N4 | note (новое) | `src/qa_tool/checks/codec/engine.py:462-463` | Журнал получает стадию `sent` и тогда, когда запрос не ушёл (`EncoderWriteRefused`, `TabooViolation`) — на итог не влияет | `sent` писать только после `write` |
| N6 | note (новое) | `src/qa_tool/checks/codec/engine.py:556-560` | Повтор эталона не снялся (камера не вернула кодек `A`) → сцена не проверяется, строка второго кодека может «пройти» без контроля S7 | без повтора — у строк кодировщика пометка «сцена не проверена» |
| N7 | note (новое) | `tests/gui/test_scenario_run.py:211` | Проверка «подпись «Картинки» скрыта без её строки» ослаблена до «в подписи нет «Картинки»» — скрытие подписи без обеих проверок больше не проверяется | добавить проверку видимости при снятых «Картинке» и «Кодеках» |
| N8 | note (новое) | `src/qa_tool/drivers/camera/encoder.py:359-375`, `tests/drivers/test_encoder.py` | Спека и «Готово, когда» T4: «токен > 64 → пропущена». Нет ни проверки, ни теста (T4 отмечен `[x]`). Инъекции нет — токен экранируется | проверка длины токена в `configs()` → строка «пропущена» + тест |
| N9 | note (новое) | `src/qa_tool/checks/codec/engine.py:450-464`; `src/qa_tool/drivers/camera/encoder.py:528-530` | `WriteReport.note` («камера даёт этот кодек только в профиле Main10») движок не читает — пометка спеки до протокола не доходит, хотя 10-битный кодек сравнивается с 8-битным эталоном | переносить `report.note` в `measure.json` и причину строки |

## Старый долг

- `src/qa_tool/checks/image/engine.py:182-193` — тот же порядок, что W2: после `settle_old` «сцена не готова» возвращает `stand_error` без строк журнала прошлого прогона (этап 7, ревьюится в `feature/image`). Гейт 3 не блокирует.

## Круг 2 (после исправлений)

Ревьюер: code-reviewer (Claude, свежий контекст) · Дифф: `git diff e71146b..0718f87` (коммит 0718f87: `src/qa_tool/checks/codec/engine.py` +17/−2, `tests/checks/test_codec_engine.py` +21) · Дата: 2026-11-10 · Круг: второй, только изменённая часть

**Вердикт:** approve по кругу 2 — B1 закрыт, новых blocker нет. Находки круга 1 W1–W8 и N1–N9 не перепроверялись и остаются открытыми.

### B1 — закрыт

- Причина устранена, а не симптом: `Curve.constant` больше не жёсткое `True`, а выводится из режима, в котором драйвер реально записал битрейт (`shot.form`, `engine.py:570`, `_constant` — `engine.py:963`). Проверка `_bitrate` в `rules.py:207` получает верный режим, и `bitrate_problem` при переменном сверяет только потолок. Источник режима — `encoder.py:535-538`: `CEILING` у Media1 и у Media2 без `ConstantBitRateSupported`.
- Соответствие спеке («постоянный — в обе стороны, переменный — только потолок»; «ступени как потолок переменного, пометка в строке»): да. Пометка «потолок переменного битрейта: постоянного камера не даёт» добавляется в причину строки при любом итоге (`engine.py:647-650`), «Ожидалось» меняется на «не выше заданного более чем на 20 %», в `measure.json` — ключ «режим битрейта» (`engine.py:706`). Обещание справки F1 (`docs/manual/codec.md:17`, «с пометкой») теперь выполняется. Список задач программистам (`Verdict.problems`) пометку не получает — ложных задач нет.
- Регрессионные тесты ловят баг: `test_variable_bitrate_below_ceiling_passes_with_note[без CBR]` и `[Media1]` (`test_codec_engine.py:143`) на коде до исправления (`engine.py` из e71146b, временно в рабочем дереве, затем возвращён из HEAD; `git status` чистый) падают с «не прошла: битрейт не соответствует заданному: … −60 % при допуске ±20 %» — ровно сценарий B1. `test_constant_bitrate_below_target_still_fails` (`:154`) проходит и до, и после: подтверждает, что постоянный битрейт по-прежнему проверяется в обе стороны (защита от перегиба фикса).
- Тексты для ОТК — по-русски, без жаргона. Комментарий над пометкой и docstring `_constant` объясняют «почему» (docs/rules/comments.md соблюдён).

### Прогоны

- `qa-test env LD_LIBRARY_PATH=… uv run pytest -q tests/checks/test_codec_engine.py tests/checks/test_codec_rules.py` → **49 passed in 208.74s**, пропусков нет (тесты `needs_vmaf` шли на настоящем libvmaf).
- Регрессия на коде до исправления: 2 failed (оба параметра нового теста), 1 passed (постоянный).
- `uv run ruff check . && uv run ruff format --check .` → All checks passed; 264 files already formatted.
- Полный прогон — по данным главной сессии (debugger): `1358 passed in 1998.38s`; ревьюером не повторялся.

### Новые находки круга 2

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| R2-1 | minor (новое) | `src/qa_tool/checks/codec/engine.py:963-967` | Режим битрейта считается на всю кривую кодека: хотя бы одна запись `CEILING` — вся кривая сверяется как переменная. Сейчас смешанного режима у одного кодека не бывает: `form` в драйвере зависит только от службы (Media1/Media2) и флага `cbr` опций этого кодека, они в прогоне не меняются. Если в будущем драйвер начнёт откатываться на переменный по отказу постоянного (решение директора 09.11, «пробовать переменный»), недобор на ступенях, записанных постоянными, будет пропущен (ложное «прошла»). Отступление от предложенного в круге 1 «режим в `Point`» | при появлении отката на переменный — хранить режим в `Point` и передавать в `_bitrate` поточечно |
| R2-2 | minor (новое) | `tests/checks/test_codec_engine.py:143-151` | Тест не проверяет «Ожидалось» («не выше заданного более чем на 20 %») и превышение потолка при переменном (переменный выше потолка + допуска обязан «не прошла»). Без второго теста можно случайно ослабить проверку потолка для `CEILING` | для test-writer: `encoder_cbr=False`, `encoder_kbps_factor=2.0` → «не прошла», в причине «допуске +20 %»; проверка `row.expected` |

Список для test-writer дополняется пунктом R2-2; п. 1 списка круга 1 закрыт новым тестом; п. 12 закрыт частично (пометка B1 проверена в причине и `measure.json`, отображение в протоколе PDF/Excel и пометка N9 — нет).

### Безопасность

Дифф круга 2 не трогает обращения к камере, `core/taboo.py`, хуки, `.claude/` и зависимости: оценки security-check.md круга 1 не меняются; agentshield и pip-audit не требовались. Snyk не запускался (заменён на pip-audit, docs/rules/security.md п. 5).

### Ждёт живых камер

Проверка B1 на живой камере без `ConstantBitRateSupported` или только с Media1 (T20) по-прежнему открыта.

Уточнение к R2-2: само правило «переменный — только потолок» в сторону превышения покрыто на уровне правил (`tests/checks/test_codec_rules.py:108-111`: 3000 кбит/с при потолке 2048 → «при допуске +20 %»). Не покрыта только связка движка `shot.form` → `Curve.constant` в сторону превышения и текст «Ожидалось».
