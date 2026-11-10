# Ревью: api-discovery

Ревьюер: независимый ревьюер, Claude Opus 5.5 · Контекст: свежий · Дифф: `git diff 1926aa6...HEAD` (feature/api-discovery, HEAD 7a7ffee) · Дата: 2026-11-10 · Круг: первый

**Вердикт:** request-changes

Найден один blocker (B1): камера отвечает Fault на одну запись автопоиска, и из-за этого пропадает вся проверка `API-MAP-1`. Воспроизведён на виртуальной камере. Табу (AD-7), вход с одной попыткой, секреты в карте и протоколе, экранирование и пути серьёзных замечаний не вызвали. Подробно — security-check.md, вердикт pass. Полный прогон тестов зелёный, линтер и проверка комментариев чистые.

## База диффа и что прочитано

- **База — `1926aa6`, а не `origin/main`.** Ветка выросла из `feature/codec-quality` (коммит 1926aa6, он предок HEAD), а та ещё не влита в main. Поэтому `git diff origin/main...HEAD` — это 279 файлов и около 49 500 строк. В них ONVIF, адаптер «Рувер», поток, веб-обход, «Картинка» и «Кодеки», которые ревьюятся в своих ветках. Собственный дифф фичи — `git diff 1926aa6...HEAD`: 33 коммита, 96 файлов, +11 488 / −60.
- Чужой код из диффа против `origin/main` читался там, где фича на него опирается: `checks/write_pass.py` (`WritePass`, `settle_old`), `core/scheduler.py` (`_report_lost_write`), `core/checks.run_check`, `drivers/camera/imaging.py` и `encoder.py` (`write`, `ValueRejected`), `drivers/camera/browser.py` (`_judge`, `stopped`), `core/secrets.redact`, `core/taboo.py` (списки слов).
- Прочитано построчно: `core/taboo.py` (новое), `drivers/camera/http.py`, `digest.py`, `native.py`, `traffic.py`, `key_value.py`, сборщики Hikvision и Dahua, возврат и отправка у «Рувер», `core/api_map.py` (модель, классификатор, разность), `checks/api/discovery.py`, `search.py`, `search_run.py`, `restore.py`, `library.py`, `limits.py`, `onvif_state.py`, `catalog.manual_pairs`, `compare.py` (начало), правки `browser.py`, `walk.py`, `login.py`, `gui/*`, `report/model.py`, `publish.py`, `packaging/smoke_onvif.py`, `tests/checks/test_api_search.py`, `test_api_discovery.py` (частично).
- Прочитано выборочно, по поиску: экранирование, `href`, `data_type`, пути — в `report/api_map.py`, `report/api_doc.py`, `report/xlsx.py`, шаблонах `_api.html` и `api_map.html`; схемы `api-map.json` и `api-pairs.json`; `tests/mock_native.py`; `docs/manual/api-map.md`. Построчно они не вычитаны. Найденное в них ограничено проверками безопасности и сверкой со спекой.

## 1. Дифф: что изменилось и почему

- T1 — `core/taboo.check_native_read`: в родной API разрешены только GET и HEAD. Опасные GET (Dahua `reboot`, `setConfig`, выгрузка конфигурации, `factory`, `upgrad`) не уходят.
- T2 — `http.request(login=HttpLogin, max_bytes=)`: Digest MD5, одна попытка, `stale` — один повтор, Basic только по HTTPS, потоковый предел размера ответа. `digest.check_challenge` проверяет недоверенный запрос входа камеры.
- T3 — `core/api_map.py`: модель карты, `api-map.json` и `api-pairs.json` версии 1, классификатор чувствительности, `diff`, `volatile_ids`. Заодно `write_journal.write_atomic` вынесен для библиотеки.
- T4 — `tests/mock_native.py`: ISAPI и CGI поверх состояния `mock_onvif`, режимы живых камер.
- T5–T9 — `native.py` (`NativeReader`, `choose_collector`), `key_value.py`, сборщики Hikvision, Dahua, «Рувер», заготовки Tiandy и Beward, производители в окне «Камеры».
- T10 — пассивный трафик `BrowserSession.traffic`, `ctx.extra[WEB_TRAFFIC_KEY]` и `drivers/camera/traffic.from_traffic`.
- T11–T13 — каталог ONVIF, ручные таблицы `data/api_pairs`, сверка `compare.py`, проверка `API-MAP-1` (`discovery.py`), пороги `api.*`, state0.
- T14–T16 — библиотека, автопоиск (`search.py`, `search_run.py`), возврат родным API (`restore.py`), журнал `API`, `ImagingWriter.verify`.
- T17–T19 — приложение протокола, документ «Карта API», Excel, строка на экране «Протоколы», справка F1, `Step.label`.
- T20 — `packaging/smoke_onvif.py`, этап `smoke_api_map`.
- T21 (ручная, открыта; её незавершённость — не находка) — исправления по живому прогону: `stream.video_source` (общий с «Картинкой», `image/engine.py`) и правило копий в `search._groups` (коммит aa2f4cd).
- **Изменения вне плана:**
  - `checks/web/login.py` — `Entry.STOPPED` (коммит 7a7ffee, cherry-pick из feature/web): вход, остановленный табу, — «ошибка стенда», признак отказа пароля не ставится. Это правка веб-модуля, а не фичи. Замечание — W1.
  - `checks/image/engine.py` — `_first_source` заменён общим `video_source`. Поведение то же, по существу замечаний нет.
  - `docs/rules/never.md` — строка про выдуманные пароли в тестах.

## 2. Соответствие спеке

| Сценарий | Реализован | Тест | Замечание |
|---|---|---|---|
| S1 карта | да | `tests/checks/test_api_discovery.py::test_hikvision_map_and_compare`, `::test_dahua_without_pairs_gives_one_skipped_match`, `::test_ruver_map_through_its_session`; `tests/drivers/test_hikvision_collector.py`, `test_dahua_collector.py`, `test_ruver_collector.py`; `tests/report/test_api_protocol.py` | — |
| S2 без сборщика, по трафику | да | `test_api_discovery.py::test_brand_without_reader_maps_web_walk_traffic`, `::test_walk_traffic_without_api_data_is_skipped_with_reason`, `::test_brands_without_reader_are_skipped`; `tests/drivers/test_traffic.py` | S-1, S-2 в security-check.md |
| S3 сверка | да | `tests/checks/test_api_compare.py` (все), `test_api_discovery.py::test_exposure_mismatch_like_111_is_a_task`, `test_api_search.py::test_bitrate_edge_like_108_reaches_range_row` | значение листа, взятое из текста `capabilities`, — m8 |
| S4 автопоиск | частично | `tests/checks/test_api_search.py::test_hikvision_pairs_found_and_everything_returned`, `::test_dahua_copies_are_one_pair`, `::test_search_refuses_settings_it_must_not_write`, `::test_same_field_at_two_addresses_is_one_parameter`, `::test_shares_pick_far_values` | Fault ONVIF на записи обрушивает всю проверку (B1). «OK и прежнее значение», бюджет, повтор с `ForcePersistence` — без тестов. Перед второй записью нет `intent` в журнале (m4) |
| S5 побочный эффект и возврат | частично | `::test_side_effect_like_111_stops_search_and_returns`, `::test_side_effect_that_onvif_cannot_return_is_returned_by_native_api`, `::test_native_return_refused_is_blocker_with_how_to_return`, `::test_old_journal_native_params_are_manual_blocker`, `tests/checks/test_api_restore.py` | «Камера пропала» (`lost_abort`, `camera_back`, `api.reconnect_wait_s`) не реализовано — входит в B1. Правило копий отошло от спеки (W3) |
| S6 без галочки | частично | `::test_without_checkbox_nothing_is_written`, `tests/checks/test_api_library.py::test_check_compares_by_library_on_next_run`, `test_api_compare.py::test_no_usable_pairs_gives_one_skipped_row` | Журнал `API` прошлого прогона без галочки не разбирается (W2) |
| S7 библиотека | да | `tests/checks/test_api_library.py` (все) | Конфликт `confirmed.json` с парой этого прогона решается не так, как в «Сверке S3» (m2) |
| S8 вход | да | `tests/drivers/test_http_login.py` (все), `test_api_discovery.py::test_refused_login_is_one_attempt_per_run`, `tests/drivers/test_native_reader.py::test_login_refusal_propagates_and_is_remembered` | На уровне проверки нет тестов «только Basic по HTTP → пропущена» и «Digest SHA-256 → ошибка стенда». Признак отказа ослаблен правкой веба (W1). MD5 вторым заголовком — S-4 |
| S9 табу | да | `tests/core/test_taboo_native.py`, `test_native_reader.py::test_dangerous_templates_never_reach_camera`, `test_api_search.py::test_search_refuses_settings_it_must_not_write`, `test_http_login.py::test_taboo_is_checked_before_any_request`, `tests/test_architecture.py` | — |

### Отклонения из progress.md — оценка

| Отклонение | Оценка |
|---|---|
| T2: драйвер не импортирует `rtsp_refusal`, связь — через `HttpLogin(already_refused, on_refused)` | принимается: слои не смешаны, поведение то же |
| T5: `names` и общая `choose_collector` вместо `matches()`; `describe_path` у шаблона | принимается: контракт проще, табу и проверка шаблона касаются и `describe_path` (`native.py:235-240`) |
| T8: `max_requests = 160` у «Рувер» | принимается: 148 операций чтения. Предел — константа кода, как велит спека |
| T10: разбор трафика в `drivers/camera/traffic.py`, а не в `core` | принимается: парсеры ответов камеры — в драйверах. Но в `ctx.extra` трафик кладётся неочищенным (S-1) |
| T12: допуск целого родного параметра ≥ 0,5 | принимается: округление шкалы |
| T14–16 (1): точка битрейта ниже предела (`EncoderKnob.below`) | принимается: живой случай .108 |
| T14–16 (2): `_shares` при равенстве берёт меньшую долю | принимается |
| T14–16 (3): журнал прошлого прогона родным API — только вручную | принимается: строже спеки, камера не трогается. Но без галочки журнал не разбирается вовсе (W2) |
| T18: без `<details>` в HTML | принимается: один шаблон для QTextDocument |
| T21: копии — по имени поля и новому значению (aa2f4cd) | **не принимается без правки спеки**: решение директора 09.11 (вопрос 6) и спека S4 п. 8 требуют «отличие только индексами». Новое правило прячет часть побочных эффектов (W3, воспроизведено) |
| Вне журнала: `Entry.STOPPED` во входе веба (7a7ffee) | см. W1 |

### Непокрытые сценарии — для `test-writer`

1. S4 п. 5: камера отвечает Fault на запись настройки по ONVIF (`ValueRejected` у картинки и кодировщика, `EncoderWriteRefused`). Ожидается строка настройки «не найдена: камера не приняла значение», возврат, следующая настройка. Строки `API-MAP-1`, `API-SEARCH-1`, `API-RESTORE-1` и сверка на месте, журнал закрыт. Сейчас тест упадёт (B1); черновик — ниже, в B1.
2. S4 п. 5: «OK и прежнее значение» (`mock_native.sticky_like_tiandy`) на уровне `API-MAP-1` — «не найдена: камера не приняла значение по ONVIF (ответ OK, читается прежнее)».
3. S4 п. 11: бюджет `api.search_budget_s` истёк — у оставшихся настроек «не пробовали: не уложились в срок автопоиска», всё возвращено.
4. S4 п. 7: Δ1 пуст — повтор с `ForcePersistence=true`, затем «не найдена: родной параметр не сдвинулся».
5. S5 «камера пропала»: `NativeUnreachable` или `CameraUnreachable` на снимке M1 или M3 посреди поиска — ожидание `api.reconnect_wait_s`, возврат, строка `API-RESTORE-1` (блокер, если не вернулось) рядом с картой и сверкой, а не одна «ошибка стенда» `API-MAP-1`.
6. S5: два разных параметра с одинаковым именем поля и одинаковым новым значением в разных блоках — побочный эффект. Например, запись ONVIF BLC (режим ON) включила `Image/channels/1/BLC/enabled` и попутно `Image/channels/1/HLC/enabled` (оба → `true`) (W3; черновик — ниже, в W3).
7. S6: журнал `API` прошлого прогона и прогон без галочки «изменяющие» — блокер `API-WRITE-LOST` «галочки нет — вернуть нельзя» (W2).
8. S8 на уровне проверки: только Basic по HTTP → `API-MAP-1` «пропущена», запросов с паролем нет; Digest `algorithm=SHA-256` → «ошибка стенда», пароль не ушёл.
9. S8 и веб: форма входа не дала понятного исхода, пароль страница отправила, а в окне входа остановлен посторонний запрос (`STOP_FOREIGN`). Признак отказа должен стоять, `API-MAP-1` не входит (W1).
10. S7: в `confirmed.json` есть пара n, а этот прогон нашёл для n другой параметр — что берётся и какая подпись (m2).

## 3. Безопасность

Вердикт security-check.md: **pass**. Четыре minor (S-1 … S-4). pip-audit локально не запускался, зависимости не менялись — CI после push человеком. agentshield — n/a: хуки, MCP и `.claude/` не менялись.

Отдельно по заданию:
- Каждый новый запрос к камере идёт через `core/taboo.py` и `drivers/camera/http.py`: да. Чтение — через `NativeReader._get` → `check_native_read` → `http.request`. Возврат — через `http.request` или сеанс `RuverClient`, который сам ходит через `http.py`. Автопоиск пишет только через `ImagingWriter` и `EncoderWriter` (`SoapClient` → `http.py`). `tests/test_architecture.py` без новых исключений и зелёный.
- Секреты не попадают в `api-map.json`, `exchanges.txt` и протокол: да, с оговорками S-2 (имена `session`/`sid` в карте по трафику) и S-3 (строки, отброшенные разбором, в сыром теле `exchanges.txt`).
- Ответы камер экранируются в HTML (autoescape, без `|safe`), в путях файлов не используются: имена доказательств — константы, папки библиотеки — `safe_name` + sha256 с `resolve()` внутри корня.
- Настоящих паролей в тестах нет: только `test-secret-N` и `wrong-9`, история чистая.

## 4. Тесты и edge cases

- Линтер: `uv run ruff check .` → `All checks passed!`; `uv run ruff format --check .` → `310 files already formatted`.
- Комментарии: `uv run python scripts/check_comments.py` → без замечаний, выход 0.
- Полный прогон: `qa-test uv run --env-file <файл: LD_LIBRARY_PATH=~/.cache/qa-tool-syslibs/root/usr/lib/x86_64-linux-gnu, QT_QPA_PLATFORM=offscreen> pytest -q -p no:cacheprovider -rf` → `1656 passed in 2083.97s (0:34:43)`, выход 0.
- Тесты фичи отдельно (19 файлов: `tests/checks/test_api_*`, `tests/core/test_api_map.py`, `test_taboo_native.py`, `tests/drivers/test_*collector*.py`, `test_http_login.py`, `test_native_reader.py`, `test_traffic.py`, `tests/report/test_api_protocol.py`, `tests/test_mock_native.py`, `tests/test_architecture.py`, `tests/checks/test_web_login.py`) → `321 passed in 105.32s`. Окно, справка F1 и браузер (`tests/gui/test_help.py`, `test_run_view.py`, `test_scenario_run.py`, `tests/drivers/test_browser.py`) → `77 passed in 99.36s`.
- Воспроизведение B1 (одиночный тест вне репозитория, через `qa-test`): `1 failed`. Вывод: `STATUS: ошибка стенда REASON: сбой проверки: ValueRejected: ter:InvalidArgVal`, `CHILDREN: []`, журнал `pending-api` остался с `stage='intent'`, `label='Резкость'`.
- Воспроизведение W3 (юнит-тест `Search._side_effect` вне репозитория): `1 failed`, `EFFECT: None` — `HLC/enabled`, сдвинувшийся вслед за записью BLC, побочным эффектом не признан.
- Не покрыто, кроме списка выше:
  - таймаут потокового чтения: у `http._send` тайм-аут httpx — на каждое чтение, а не на весь ответ, бюджет карты проверяется только между запросами; камера, отдающая по байту раз в 4 с, держит запрос минуты;
  - `nonce` истёк без `stale=true` (m6);
  - Digest `uri` с `[` `]` в запросе возврата Dahua (m5);
  - рост библиотеки (m7);
  - `smoke_api_map` без папки прогона падает `IndexError`, а не пишет проблему (m10).

## Находки

Происхождение у всех находок ниже — «новое» (внесено этим диффом). Старый долг, найденный по пути, — в последнем разделе.

### Blocker

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| B1 | blocker | `src/qa_tool/checks/api/search.py:633-640`, `src/qa_tool/checks/api/search_run.py:301`, `src/qa_tool/checks/api/discovery.py:79-87, 225-234`; `src/qa_tool/checks/api/limits.py:23` | **Отказ камеры на одной записи автопоиска уничтожает всю проверку `API-MAP-1`.** `ImagingKnob.write` и `EncoderKnob.write` не ловят `ValueRejected` (Fault ONVIF на поле и на полном блоке) и `EncoderWriteRefused` (драйвер бросает его, например, при смене кодека на Media1; достижимость этого случая из автопоиска не проверялась). `Search.run` ловит любое исключение, пробует `knob.restore()` и пробрасывает дальше. `run_search` и `Discovery.run` его не ловят: в `ACCESS_ERRORS` этих классов нет. В итоге `run_check` выдаёт одну строку «ошибка стенда: сбой проверки: ValueRejected…». **Что ломается:** пропадают строки `API-MAP-1` (карта, `api-map.json`, `exchanges.txt`), вся сверка `API-MATCH`/`API-RANGE` (включая расхождение .111, ради которого фича), пары, уже найденные этим прогоном, строки `API-SEARCH-1` и `API-RESTORE-1`. Библиотека не пишется, документ «Карта API» не выпускается. Журнал `pending-api` остаётся открытым, и в конце прогона планировщик добавляет блокер `API-WRITE-LOST` (`scheduler.py:152-154`), даже если `knob.restore()` всё вернул. ОТК получает ложный блокер вместо карты. Тот же путь у пропавшей камеры (`CameraUnreachable`, `NativeUnreachable` на снимке): спека («Побочный эффект…»: «Камера пропала: `lost_abort`, `camera_back(api.reconnect_wait_s)`, затем возврат») не реализована — `api.reconnect_wait_s` объявлен, но нигде не используется. **Когда:** камера объявляет диапазон в GetOptions, но отвечает Fault на значение внутри него. Это обычное поведение: спека S4 п. 5 разбирает этот случай отдельно («Fault … → не найдена, возврат, следующая»), а `checks/image/engine.py:376` ловит тот же `ValueRejected`. Смена кодека с Fault (H.265 недоступен при текущем разрешении) — тот же путь. **Воспроизведено** на `tests/mock_native.hikvision` + `MockCamera` с подменой `ImagingWriter.write` для `sharpness`: `CHILDREN: []`, журнал не закрыт. Тест — ниже | В `Search._try` ловить `ValueRejected`, `EncoderWriteRefused` и `ImageWriteRefused` вокруг `knob.write` → «не найдена: камера не приняла значение по ONVIF», возврат, следующая настройка. Для `CameraUnreachable`, `TransportError` и `NativeUnreachable` посреди поиска — `camera_back(api.reconnect_wait_s)`, возврат по ONVIF, затем строки `API-SEARCH-1` и `API-RESTORE-1` (блокер, если не вернулось) с уже собранной картой и сверкой, а не проброс. Тесты — п. 1 и п. 5 списка для `test-writer` |

Черновик теста для B1 (в `tests/checks/test_api_search.py`, фикстура `run` и помощники оттуда же):

```python
def test_onvif_fault_on_one_setting_is_not_found_and_search_goes_on(run, tmp_path, monkeypatch):
    from qa_tool.drivers.camera import imaging
    from qa_tool.drivers.camera.soap import OnvifFault

    original = imaging.ImagingWriter.write

    def write(self, key, value, persist=False):
        if key == "sharpness":
            raise imaging.ValueRejected(OnvifFault("ter:InvalidArgVal"))
        return original(self, key, value, persist)

    monkeypatch.setattr(imaging.ImagingWriter, "write", write)
    result, camera = run(hikvision(password=SECRET))
    found = rows(result)
    assert {"API-MAP-1", "API-SEARCH-1", "API-RESTORE-1"} <= set(found)
    assert found["API-RESTORE-1"].result.status is Status.PASSED
    assert 1 in pairs_of(result)  # яркость найдена до резкости и не потеряна
    assert load(tmp_path / "pending-api", "127.0.0.1", camera.port) is None
```

### Warning (major)

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| W1 | major (нужна проверка: живой камеры, которая молча отвергает пароль и в окне входа шлёт запрос, остановленный браузером) | `src/qa_tool/checks/web/login.py:183-191` (коммит 7a7ffee, вне плана) | Любой запрос из `session.stopped` в окне входа превращает неясный исход («форма осталась на экране») в `Entry.STOPPED` без признака отказа. Комментарий утверждает: «пароль до камеры не дошёл». Код это не проверяет. В `stopped` попадают и `STOP_FOREIGN` (чужой хост — шрифт, CDN, аналитика), и пятая запись сверх лимита окна (`browser.py:607-608, 649-656`). **Сценарий:** страница отправила пароль (разрешённый POST окна), камера отказала без 401/403 и без узнаваемых слов, страница подгрузила внешний ресурс. Признак отказа не ставится, и `API-MAP-1`, «Картинка», «Кодеки» и стресс пробуют тот же неверный пароль ещё раз. Это противоречит S8 и never.md («после отказа … не повторять вход»). Hikvision блокирует вход после `maxIllegalLoginTimes` | `STOPPED` — только если в окне не ушло ни одного изменяющего запроса (счётчик `_window_writes` не тронут) и остановленный запрос — табу или опасное действие, а не `STOP_FOREIGN`. Иначе — прежний `_refused`. Тест — п. 9 списка |
| W2 | major | `src/qa_tool/checks/api/discovery.py:328-330`, `src/qa_tool/checks/api/search_run.py:220-247`; `docs/manual/api-map.md:119` | Журнал `API` прошлого прогона разбирается только внутри `run_search`, то есть только с галочкой «изменяющие». Спека («Проход»: «вход → журнал прошлого прогона (`settle_old`) → карта M0 …») ставит этот шаг до ветвления по галочке, и справка обещает это без оговорок («следующий прогон сначала разбирает журнал»). `WritePass.settle_old` без галочки сам даёт блокер «галочки нет — вернуть нельзя». Планировщик сообщает только о журналах своего прогона (`scheduler.py:389`). **Сценарий:** программа упала или её закрыли посреди автопоиска, и `_report_lost_write` не успел. Следующий прогон без галочки: `API-MAP-1` «прошла», про изменённую камеру — ни слова. Сверка `API-MATCH` при этом идёт по сдвинутым значениям | Вызывать `WritePass(ctx, API).settle_old(...)` в `Discovery._result` до сверки независимо от галочки. Без галочки `settle_old` уже даёт нужную строку. Тест — п. 7 списка |
| W3 | major (воспроизведено юнит-тестом; живой камеры с таким эффектом не было) | `src/qa_tool/checks/api/search.py:327-347` (коммит aa2f4cd) | Копии одного параметра теперь — «то же имя поля и то же новое значение» в любом месте карты. Спека S4 п. 8 и решение директора 09.11 (вопрос 6): «одно имя, отличие **только индексами**, одно новое значение». Общие имена полей (`enabled`, `mode`, `level`) в разных блоках сливаются в одну группу. **Сценарий:** запись ONVIF BLC (уровень, режим ON) — в ONVIF изменились 2 листа (`Mode`, `Level`). В ISAPI сдвинулись `BLC/BLCLevel`, `BLC/enabled` → `true` и, как побочный эффект, `HLC/enabled` → `true`. Групп 2 (`blclevel`, `enabled`), записанных листов 2 — `Search._side_effect` возвращает `None`, `API-SEARCH-1` «прошла» вместо «не прошла» (S5). Возврат и сверка всей карты это ловят, так что камера изменённой не останется. Но ОТК теряет находку S5. Спека не обновлена, а это решение директора | Сливать копии по разным адресам только внутри одного раздела родного API или по явному списку дублей сборщика («Рувер»: `/api/image` и `/api/image/<имя>`). Иначе — правило спеки. Если директор согласует новое правило — записать его в спеку. Тест — п. 6 списка, черновик ниже |

Черновик теста для W3 (в `tests/checks/test_api_search.py`):

```python
def test_same_field_name_in_another_block_is_a_side_effect():
    from qa_tool.checks.api.catalog import BY_N
    from qa_tool.checks.api.search import Limits, Search
    from qa_tool.core.api_map import Change, NativeMap, Section

    class Knob:
        entry = BY_N[11]  # BLC: уровень

        def mine(self):
            return {"BacklightCompensation/Level", "BacklightCompensation/Mode"}

        def drift(self):
            return []

    native = NativeMap(brand="hikvision", collector="ISAPI")
    section = Section("Image/channels/1", "картинка", "GET /ISAPI/Image/channels/1")
    for pid in ("BLC/BLCLevel", "BLC/enabled", "HLC/enabled"):
        section.add_param(f"Image/channels/1/{pid}", value="x")
    native.sections.append(section)
    search = Search(
        [],
        lambda: native,
        native,
        frozenset(),
        lambda _k: ("Image/channels/*",),
        lambda *a: None,
        Limits(),
        "",
        "ISAPI",
    )
    changes = [
        Change("Image/channels/1/BLC/BLCLevel", "50", "80"),
        Change("Image/channels/1/BLC/enabled", "false", "true"),
        Change("Image/channels/1/HLC/enabled", "false", "true"),
    ]
    before = {"BacklightCompensation/Mode": "OFF", "BacklightCompensation/Level": "50"}
    after = {"BacklightCompensation/Mode": "ON", "BacklightCompensation/Level": "80"}
    assert search._side_effect(Knob(), changes, before, after, native) is not None
```

### Minor

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| m1 | minor | `src/qa_tool/checks/api/search_run.py:302-310` и `src/qa_tool/core/scheduler.py:152-154, 389` | Когда `API-RESTORE-1` «не прошла», журнал остаётся открытым (так и задумано), и в конце прогона планировщик добавляет ещё и `API-WRITE-LOST` — в протоколе два блокера на одно событие. Шаблон взят у «Картинки» (старый долг), здесь повторён заново | После строки `API-RESTORE-1` пометить журнал `reported = True` (как `web/write.py:279`) |
| m2 | minor | `src/qa_tool/checks/api/library.py:143-160` | Спека, «Сверка S3»: «найденная и ручная разошлись — берётся ручная». В `resolve` ручная — только `data/api_pairs`. Пару из `confirmed.json`, подтверждённую человеком, найденная в этом прогоне молча перекрывает, без пометки «прогон … нашёл иначе». При этом справка `api-map-library` учит исправлять пары правкой `confirmed.json` | В `older` учитывать и `library.confirmed`. Тест — п. 10 списка |
| m3 | minor | `src/qa_tool/checks/api/discovery.py:304-306` | Причина дублируется: «пароль ушёл бы открытым текстом: камера принимает только Basic по HTTP — пароль ушёл бы открытым текстом» | Выдавать `str(error)` без приставки |
| m4 | minor | `src/qa_tool/checks/api/search.py:498-501` | Перед второй записью `v2` нет `intent`: журнал до неё говорит `new = v1`, а `sent` с `v2` пишется уже после записи. Если процесс снят между записью и `note`, следующий прогон прочитает `v2 ≠ new`, решит «значение уже не наше» и выдаст ручной блокер вместо автоматического возврата. Спека S4 п. 5: «журнал intent → запись → sent» | `self.note(entry, str(v0), str(v2), "intent", knob.journal())` перед `knob.write(v2)` |
| m5 | minor (нужна проверка: живая Dahua с возвратом родным API) | `src/qa_tool/drivers/camera/http.py:81-88` | Digest `uri` строится из сырого `parts.query`. httpx может отправить `[` `]` в запросе `setConfig&VideoColor[0][0].Brightness=` как `%5B%5D`. Если Dahua сверяет `uri` со строкой запроса, будет 401, `login.refuse`, и признак отказа пароля встанет на весь прогон из-за возврата. Mock сравнивает после раскрытия %XX, поэтому тесты этого не видят. В журнале T4 это отмечено «проверить в T21», но в T21 возврат родным API не срабатывал | Брать `uri` из `httpx.URL(url).raw_path` — то, что реально уходит в строке запроса |
| m6 | minor (нужна проверка) | `src/qa_tool/drivers/camera/http.py:187-193` | 401 на подписанный запрос без `stale=true` (камера, которая не ставит `stale` при истёкшем `nonce`) считается отказом пароля на весь прогон | Один раз начать заново без запомненного `challenge` и только после второго 401 считать отказ. Либо записать это ограничение в спеку |
| m7 | minor | `src/qa_tool/checks/api/library.py:163-179`, `src/qa_tool/checks/api/discovery.py:344-346` | Каждый прогон пишет в библиотеку `map-<прогон>-<место>.json` (до 8 МБ) и `pairs-*.json`, удаления нет. Читаются только 50 последних `pairs-*`. Диск стенда растёт без предела | Хранить N последних карт на модель или писать карту только при найденных парах |
| m8 | minor | `src/qa_tool/drivers/camera/hikvision/collector.py:216-220, 267-276` | Значение листа, которого нет в ресурсе настроек, берётся из текста `capabilities`. В карте и в сверке такое значение выглядит как текущее, а попадая в тело PUT возврата (поддерево M0), может записать камере значение из описания, а не из настроек | Помечать источник значения. В поддерево возврата и в сверку брать только листья ресурса настроек |
| m9 | nit | `src/qa_tool/drivers/camera/browser.py:193` | `TRAFFIC_MAX_ENTRIES = 400`, в спеке ≤ 500 | Сверить число со спекой |
| m10 | nit | `packaging/smoke_onvif.py:121` | `runs[-1]` вычисляется до проверки `not runs`: без папки прогона проверка сборки падает `IndexError` вместо строки проблемы | Вычислять `documents` только при непустом `runs` |

### Старый долг (Гейт 3 не блокирует)

- `src/qa_tool/checks/write_pass.py` и `src/qa_tool/checks/image/engine.py`: после блокера возврата журнал не помечается `reported`, и планировщик дублирует блокер в конце прогона. Тот же шаблон перенесён сюда (m1).
- `src/qa_tool/core/scheduler.py:385-390`: журналы прошлых прогонов планировщик не показывает. Их разбирает сама проверка, и только когда она запущена. Для «Картинки» и «Кодеков» без галочки это означает тишину. Для `API-MAP-1` — W2.

## Круг 2 (после 36e555e)

Ревьюер: независимый ревьюер, Claude Opus 5.5 · Контекст: свежий · Дифф: `git show 36e555e` (`checks/api/search.py`, `checks/api/search_run.py`, `tests/checks/test_api_search.py`; +179 / −20) · Дата: 2026-11-10 · Круг: второй, только изменённая часть

**Вердикт:** request-changes

Фикс закрывает B1 для двух случаев: Fault ONVIF на записи одной настройки и пропажа камеры на записи. Ожидание `api.reconnect_wait_s` подключено. Открытым остался соседний путь того же сбоя: Fault на возврате по ONVIF по-прежнему уничтожает всю `API-MAP-1` (B2, воспроизведено). Он достижим и через новую ветку «камера вернулась»: TCP-порт уже открыт, а ONVIF после перезагрузки ещё отвечает 503. Новых путей к камере в обход `core/taboo.py` нет. Отказ пароля не глотается. «Ошибка стенда» в задачу не превращается.

### Что изменилось

- `Search.run` разбирает исключения записи так:
  - `REJECTED` (`ValueRejected` картинки и кодировщика) → «не найдена: камера не приняла значение по ONVIF: …»;
  - `NOT_SENT` (`ImageWriteRefused`, `EncoderWriteRefused`) → «пропущена: запись не отправлена»;
  - `UNREACHABLE` (`CameraUnreachable`, `NativeUnreachable`, `TransportError`) → `out.lost`, возврат, поиск прерван;
  - прочее, как раньше, → `knob.restore()` и проброс.
- `Search._returned`: если камера пропала, перед возвратом `camera_back()`. Не вернулась — `out.unchecked`, возврат не делается, журнал остаётся. Пропала на самом возврате — одно ожидание, второй раз — `unchecked`.
- `run_search`:
  - `camera_back` = `WritePass.lost = True` + `camera_back(api.reconnect_wait_s)`;
  - `restored` учитывает `unchecked`, журнал закрывается только при сверенном возврате;
  - `UNREACHABLE` на исходных снимках M0a и M0b → `API-SEARCH-1` «ошибка стенда» (мелкая), карта и сверка остаются.
- `_search_row`: при `lost` — «не прошла», причина `write_pass.LOST`. `_restore_row`: при `unchecked` — «не прошла», блокер.
- Тесты:
  - `test_onvif_fault_on_one_setting_is_not_found_and_search_goes_on` — черновик B1, усилен проверкой пары 15 и `search.json`;
  - `test_camera_lost_for_a_moment_stops_search_and_returns`;
  - `test_camera_gone_keeps_map_and_journal_and_is_restore_blocker` — ожидание `api.reconnect_wait_s` = 0,05 с, текст «не вернулась за 0.05 с», журнал с `kind == "4"`.
- Вне плана изменений нет. Незакоммиченная правка `review.md` в рабочей копии — только форматирование ruff черновика W3.

### Проверки по заданию

| Вопрос | Итог | Основание |
|---|---|---|
| B1 решён по S4 п. 5 | да для Fault на записи | `search.py:651-654`. Тест 1 падает на родителе 36e555e (воспроизведение круга 1: `CHILDREN: []`) и проходит сейчас. Отдельно проверено: `ValueRejected` у кодировщика на каждой записи и Fault на второй записи `v2` дают `API-RESTORE-1` «прошла», журнал закрыт |
| B1 решён по S5 «камера пропала» с `api.reconnect_wait_s` | да для пропажи на записи; **нет** для Fault на возврате (B2) | `search.py:655-657, 691-712`, `search_run.py:299-301`. Тест 3 проверяет порог по тексту причины. Пропажа на снимках M1 и M3 (`NativeUnreachable`) идёт тем же путём по коду, но тестов нет |
| Новые пути к камере в обход `core/taboo.py` | нет | В `search.py` новые импорты — только классы исключений. `camera_back` → `WritePass._wait_for_camera` → `core/worker.camera_alive`: TCP-порт камеры со стенда, код существовал и раньше. `tests/test_architecture.py` зелёный |
| Журнал `pending-api` закрывается во всех ветках | да, кроме B2 | Закрывается: Fault на записи, `NOT_SENT`, пропажа с возвратом камеры. Остаётся, как велит спека: камера не вернулась. Остаётся ошибочно: Fault на возврате (`stage='restoring'`, B2) и прочие исключения (W4) — в конце прогона это даёт `API-WRITE-LOST` вместо карты |
| Ложные или двойные блокеры | двойной остался (m1, теперь и на `unchecked`) | При `unchecked` журнал не помечается `reported`. Планировщик (`scheduler.py:389`) добавит `API-WRITE-LOST` к `API-RESTORE-1`. Спека S5: «не вернулась — `API-RESTORE-1` блокер; процесс снят — `API-WRITE-LOST`», то есть два блокера на одно событие против спеки. Блокер при этом не теряется, поэтому уровень прежний — minor |
| «Ошибка стенда» → задача программистам | нет | Исходные снимки → `stand_error`. Пропажа посреди поиска → «не прошла» (мелкая), как в «Картинке»: спека, таблица «камера пропала — как в «Картинке» — не прошла / ошибка стенда». `REJECTED` — только итог настройки в трассе, `API-SEARCH-1` «прошла» |
| Глотаются ли исключения, которые должны валить проверку | нет | `LoginRefused` не входит ни в один кортеж: проверено вне репозитория, итог — «ошибка стенда: вход в родной API отвергнут». Fault отказа пароля ONVIF драйверы не заворачивают в `ValueRejected` (`imaging.py:374-384`, `encoder.py:572-575`, `credentials_rejected`), поэтому `REJECTED` его не ловит. Иерархия: `CameraUnreachable(ConnectionError)`, `NativeUnreachable(RuntimeError)`, `httpx.TransportError`; `LoginRefused` ни от одного из них не наследуется |
| Тесты ловят баг | да для записи | Тест 1 — точный черновик B1. Тест 3 останавливает mock-камеру по-настоящему и проверяет журнал. Не покрыто — ниже |

### Новые находки

Происхождение «новое» — внесено диффом фичи `1926aa6...HEAD`. Отдельно помечено, внёс ли путь сам 36e555e.

| # | Уровень | Где | Проблема и риск | Минимальный фикс |
|---|---|---|---|---|
| B2 | blocker | `src/qa_tool/checks/api/search.py:691-712` (`_returned` ловит только `UNREACHABLE`), `:714-732` (`_put_back`: `knob.restore()` на `:717`, `knob.verify()`) | **Fault на возврате по ONVIF уничтожает всю `API-MAP-1`, как B1.** `ImagingWriter.restore` документированно бросает `OnvifFault`, `EncoderWriter.restore` — `ValueRejected`. Оба выходят из `_returned` и `Search.run` (вызов `:664` стоит вне `try`), затем из `run_search` и `Discovery`. Итог — «ошибка стенда: сбой проверки: OnvifFault…», строк нет, журнал `stage='restoring'` открыт, в конце прогона приходит `API-WRITE-LOST`. Пропадают карта, сверка (расхождение .111), найденные пары, `API-SEARCH-1` и `API-RESTORE-1`. **Сценарии.** (1) Внесён этим фиксом. Камера перезагрузилась посреди поиска. `camera_back` проверяет только TCP-порт (`camera_alive`), порт открылся раньше службы ONVIF, и `SetImagingSettings` получает 503 или HTML. `soap.py:216-218` превращает это в `OnvifFault("ответ не SOAP (HTTP 503)")`, а не в `CameraUnreachable`. Это ровно тот случай, ради которого ждут `api.reconnect_wait_s`. (2) Был до фикса. Камера отвечает Fault на полный блок картинки или на `ForcePersistence=true` при возврате. «Картинка» этот случай ловит (`image/engine.py:421`). **Воспроизведено** вне репозитория (`/tmp`, через `qa-test`): `ValueRejected` на записи резкости + `OnvifFault` на её возврате → `STATUS: ошибка стенда`, `CHILDREN: []`, `JOURNAL: ('restoring', 'Резкость')` | В `_returned` ловить `OnvifFault` (кроме `credentials_rejected` — его пробрасывать), `REJECTED` и `NOT_SENT` вокруг `_put_back` → `out.unchecked = f"возврат по ONVIF не подтверждён: {error}"`, вернуть `[], []`. В `run` прерывать поиск и при `out.unchecked` (`:679`), не только при `stop` и `lost`: состояние камеры неизвестно. Тест — черновик ниже |
| W4 | major | `src/qa_tool/checks/api/search.py:658-663` | **Прочие исключения внутри `_try` по-прежнему уносят `API-MAP-1`** (вопрос главной сессии). Самое вероятное — разовый Fault на чтении: `knob.leaves()` → `GetImagingSettings`, `read_value` после записи, `EncoderWriter.read`. Это `OnvifFault`, а не `ValueRejected`, поэтому он попадает в `except Exception: … raise`. **Воспроизведено:** один `OnvifFault("ter:Action")` на `ImagingWriter.read` после записи резкости → `CHILDREN: []`, журнал `intent` открыт. Не blocker, потому что разовый Fault на чтении у камеры, которая только что ответила на то же чтение, — случай редкий. Но последствия те же, что у B1, а «Картинка» закрывает его общим правилом (`image/engine.py:228`: «сбой на одной настройке не должен унести строки и возврат») | Ловить `OnvifFault`, кроме `credentials_rejected`, как итог настройки «ошибка стенда на чтении: …», затем `_returned` и остановка поиска. Пробрасывать только отказ входа (`LoginRefused`, Fault отказа пароля) и `TabooViolation`. Тест — п. 3 списка ниже |
| m11 | minor (нужна проверка: живая Hikvision или Dahua после перезагрузки — ставит ли `stale=true` на старый `nonce`) | `src/qa_tool/checks/api/search_run.py:299-301`; `src/qa_tool/drivers/camera/http.py:187-193` | Внесено фиксом. После `camera_back` снимки и возврат родным API идут со старым запросом входа Digest (`HttpLogin.challenge`). Перезагруженная камера `nonce` не знает. Если она отвечает 401 без `stale=true`, `http.py` считает это отказом пароля (m6 круга 1). `on_refused` ставит признак отказа на весь прогон, `LoginRefused` выходит из поиска, и `API-MAP-1` становится «вход отвергнут» без карты. Остальные проверки камеры пароль больше не пробуют. Повторного неверного пароля нет, так что это вопрос доступности, а не блокировки учётной записи | В `camera_back` после «вернулась» сбрасывать `challenge` и `count` у общего `HttpLogin`: первый запрос уйдёт без подписи, и 401 на него отказом не считается. Тест: mock с новым `nonce` после остановки и запуска |
| m12 | minor | `src/qa_tool/checks/api/search.py:704-712, 723` | Внесено фиксом. Если камера пропала на последнем снимке `_put_back` (`:732`) уже после `self.restorer(...)`, цикл `_returned` после ожидания повторяет весь `_put_back`, и `NativeRestorer.run` отправляет запросы родного API второй раз. Спека S5: «один `restore_request`», в строке возврата — «программа пробовала один раз» | Запоминать, что `restorer` уже отправлял (`out.attempts` с `sent`), и при повторе только перечитывать |
| m1 | minor (перенос, путь расширен фиксом) | `src/qa_tool/checks/api/search_run.py:327-331`; `src/qa_tool/core/scheduler.py:389` | См. круг 1. Теперь двойной блокер выходит и при `unchecked` (камера не вернулась), а спека S5 здесь называет только `API-RESTORE-1` | Как в круге 1: после строки `API-RESTORE-1` «не прошла» пометить журнал `reported = True` |
| m13 | nit | `src/qa_tool/checks/api/search.py:46`, `src/qa_tool/checks/write_pass.py:33`, `src/qa_tool/checks/api/search_run.py:20, 87-89` | Две разные константы `LOST` с одним именем. `search_run` берёт причину строки из `write_pass.LOST`, а текст `actual` — из `search.LOST`. Легко перепутать при правке | Переименовать `search.LOST` (например, `SEARCH_LOST`) |
| m14 | nit | `src/qa_tool/checks/api/search.py:474, 653-654, 755-766` | При `NOT_SENT` на первой записи в трассе стоит `записи: [v0, v1]` и «возвращено, карта совпала», хотя `v1` не уходил | Записывать `v1` в `_facts["записи"]` после `knob.write`, а не до |
| m15 | nit (старое в фиче, фиксом не внесено) | `src/qa_tool/checks/api/search_run.py:85, 100` | «Найдено пар 11 из 10 настроек» (видно в прогоне вне репозитория). Пары режима из `_mode_pair` считаются вместе с настройками | Считать только пары пробованных настроек или писать «найдено пар K, из них режимов M, по L настройкам» |

Черновик теста для B2 (в `tests/checks/test_api_search.py`, фикстура `run` и помощники оттуда же):

```python
def test_onvif_fault_on_return_is_restore_blocker_and_map_stays(run, tmp_path, monkeypatch):
    from qa_tool.drivers.camera import imaging
    from qa_tool.drivers.camera.soap import OnvifFault

    original_write = imaging.ImagingWriter.write
    original_restore = imaging.ImagingWriter.restore
    written = []

    def write(self, key, value, persist=False):
        written.append(key)
        return original_write(self, key, value, persist)

    def restore(self, state, touched):
        if "sharpness" in written:
            # камера после перезагрузки: порт открыт, ONVIF ещё отвечает 503
            raise OnvifFault("ответ не SOAP (HTTP 503)", status=503)
        return original_restore(self, state, touched)

    monkeypatch.setattr(imaging.ImagingWriter, "write", write)
    monkeypatch.setattr(imaging.ImagingWriter, "restore", restore)
    result, camera = run(hikvision(password=SECRET))
    found = rows(result)
    assert {"API-MAP-1", "API-MATCH-1", "API-SEARCH-1"} <= set(found)
    restore_row = found["API-RESTORE-1"]
    assert restore_row.result.status is Status.FAILED
    assert restore_row.severity is Severity.BLOCKER
    assert load(tmp_path / "pending-api", "127.0.0.1", camera.port) is not None
```

### Непокрытые сценарии — для `test-writer` (круг 2)

1. B2: Fault ONVIF на возврате (картинка — `OnvifFault`, кодировщик — `ValueRejected`), в том числе 503 после `camera_back`. Ожидается `API-RESTORE-1` блокер, карта и сверка на месте, журнал открыт. Сейчас тест падает; черновик выше.
2. S5 «камера пропала» на снимке, а не на записи: `NativeUnreachable` на M1 (`_try`) и на M3 (`_put_back`) — ожидание, возврат, `API-SEARCH-1` «не прошла». По коду работает, тестов нет.
3. W4: разовый `OnvifFault` на `GetImagingSettings` или `GetVideoEncoderConfiguration` посреди поиска.
4. Повторная пропажа на возврате после того, как камера вернулась: `unchecked` «возврат не сверен: …», второго ожидания нет (`search.py:707-712`).
5. `NOT_SENT`: `EncoderWriteRefused` на смене кодека → «пропущена: запись не отправлена», поиск идёт дальше, журнал закрыт.
6. `ValueRejected` у кодировщика (битрейт). Вне репозитория проверено, что проходит; в наборе теста нет.
7. `UNREACHABLE` на исходных снимках M0a и M0b → `API-SEARCH-1` «ошибка стенда», `API-RESTORE-1` нет, карта и сверка на месте (`search_run.py:270-278`).
8. m11: Digest-камера после перезагрузки (новый `nonce` без `stale=true`) — признак отказа пароля не ставится.

Пункты круга 1: п. 1 закрыт тестом 1. П. 5 закрыт частично — пропажа на записи покрыта, на снимках нет (п. 2 выше). Остальные — открыты.

### Статус находок круга 1

| # | Статус |
|---|---|
| B1 | закрыт частично: Fault на записи и пропажа на записи — да; Fault на возврате → B2, прочие исключения → W4 |
| W1, W2, W3 | открыты, фикс их не касается |
| m1 | открыт, путь расширен (см. выше) |
| m2–m10 | открыты, фикс их не касается |
| S-1 … S-4 (security-check.md) | открыты, фикс их не касается |

### Тесты и проверки круга 2

- Тесты фичи: `qa-test uv run --env-file <LD_LIBRARY_PATH, QT_QPA_PLATFORM=offscreen> pytest -q -p no:cacheprovider tests/checks/test_api_search.py test_api_restore.py test_api_discovery.py test_api_catalog.py test_api_compare.py test_api_library.py test_api_ruver.py tests/test_architecture.py` → `140 passed in 125.11s`.
- Полный прогон не повторялся. По данным главной сессии (debugger): `1659 passed in 1956.67s`, выход 0.
- Линтер: `uv run ruff check .` → `All checks passed!`; `uv run ruff format --check .` → `312 files already formatted`.
- Комментарии: `uv run python scripts/check_comments.py` → без замечаний, выход 0. Новые комментарии диффа объясняют «почему» (S4 п. 5, «камеру уже ждали…»). Пересказа кода и английских комментариев нет.
- Воспроизведения вне репозитория (`/tmp`, через `qa-test`, mock-камера, реальные камеры не трогались): 5 сценариев.
  - Fault на возврате — B2, `1 failed`.
  - Fault на чтении — W4, `1 failed`.
  - `ValueRejected` кодировщика — `passed`.
  - `ValueRejected` на второй записи — `passed`.
  - `LoginRefused` на записи не глотается — `passed`, итог «ошибка стенда: вход в родной API отвергнут».
