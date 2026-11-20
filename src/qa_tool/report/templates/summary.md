# Сводный отчёт прогона № {{ s.run_id }}

Начат {{ s.started }} · окончен {{ s.finished }} · место стенда: {{ s.place }} · qa-tool {{ s.version }}

## 1. Камеры

| Место | Протокол | Камера | Прошивка | Прошла | Не прошла | Ошибка стенда | Пропущена | Заключение |
|---|---|---|---|---|---|---|---|---|
{% for p in s.protocols %}
| {{ p.slot }} | {{ p.protocol_id }} | {{ p.camera_name }} | {{ p.passport.firmware }} | {{ p.counts["прошла"] }} | {{ p.counts["не прошла"] }} | {{ p.counts["ошибка стенда"] }} | {{ p.counts["пропущена"] }} | {{ p.verdict }} |
{% endfor %}

## 2. Сравнение: проверка × камера

| Проверка |{% for p in s.protocols %} {{ p.slot }}. {{ p.camera_name }} |{% endfor %}

|---|{% for p in s.protocols %}---|{% endfor %}

{% for check_id, title in s.checks %}
| **{{ check_id }}** {{ title }} |{% for p in s.protocols %} {{ s.matrix[check_id].get(p.slot, "—") }} |{% endfor %}

{% endfor %}

{% if s.image_compare %}
Сравнение картинки — image_compare.html (а также .pdf и .xlsx).

{% endif %}
{% if s.codec_compare %}
Сравнение кодеков — codec_compare.html (а также .pdf и .xlsx).

{% endif %}
## 3. Задачи программистам по камерам

| Место | Камера | Задач | Из них блокеров |
|---|---|---|---|
{% for p in s.protocols %}
| {{ p.slot }} | {{ p.camera_name }} | {{ p.tasks | length }} | {{ p.tasks | selectattr("severity", "equalto", "блокер") | list | length }} |
{% endfor %}
