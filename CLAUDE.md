# CLAUDE.md — агент сбора детских/семейных событий (Бразилия)

Проект: ежедневный сбор детских и семейных событий. Пилот — **Sympla, São Paulo**,
цель пилота — 100 событий в целевой схеме (ниже). Основной файл: `sympla_agent.py`.

## Архитектурный принцип (КРИТИЧНО — не нарушать)
1. **Факты приносит только детерминированный код** (HTTP/парсинг/официальный API).
   Каждая запись обязана ссылаться на реально открывающийся `source_url`.
2. **LLM классифицирует ПОВЕРХ уже добытого текста и НЕ выдумывает факты.**
   Если данных не хватает — `null`, а не догадка.
3. **Перед записью — валидация.** Не прошло — `status: "требует проверки"`, не выкидывать.

> Контекст: на этапе разведки чат-модель нагенерировала несуществующие события Eventbrite
> (фейковые id/URL), внешне неотличимые от настоящих. Этот пайплайн построен, чтобы такое
> не попадало в базу. Никогда не просить модель «собрать события» — только классифицировать.

## Доступ к источникам (состояние 2026)
- Открытого «найти все события» API нет ни у кого.
- Sympla: публичный API отдаёт только события владельца токена; широкий доступ — через
  **партнёрскую API** (партнёрство в процессе оформления, не готово).
- **Пилот идёт через парсинг публичной афиши Sympla без логина.** Уважать `robots.txt`,
  держать вежливый rate-limit, представляться честным User-Agent. Только публичные данные.

## Метод извлечения
- **JSON-LD first**: на странице события парсить `<script type="application/ld+json">`
  (schema.org/Event) — оттуда `name`, `startDate`, `endDate`, `location`, `offers/price`,
  `eventStatus`, `inLanguage`, `eventAttendanceMode`. Это надёжнее и стабильнее HTML.
- **HTML-фоллбэк** — только если JSON-LD неполный.

## Целевая схема факта (20 полей)
title, description, start_date, start_time, end_date, end_time, address, city, district,
age_category, price, is_free, source_url, category, format (offline|online), language,
suitable_for_children, suitable_for_parents, updated_at,
status ∈ {новое, актуальное, скоро начнётся, прошло, отменено, недоступно, требует проверки}

Раскладка по способу получения:
- **Прямо (JSON-LD/страница):** title, description, start/end date+time, address, price, source_url.
- **Нормализация:** city, district, is_free, category, format, currency.
- **Дериватив (LLM/правило):** age_category, language, suitable_for_children/parents, status, updated_at.

## Состояние реализации (источники подтверждены живой инспекцией Sympla)
Sympla — Next.js/SPA: JSON-LD на страницах НЕТ, листинг рендерится клиентом.
Поэтому факты берём из трёх реальных эндпоинтов/встроенного JSON:
- **Discovery — `search_events()` / `discover_event_urls()`**: search-API
  `GET /api/discovery-bff/search/category-type?publics=97,220&city=São Paulo&page=N`
  → `{"data":[…],"total","limit","page"}`. Пагинация по `page`, дедуп по `url`.
  `fact_from_search()` строит базовый факт прямо из ответа (name, дата из
  `*_date_formats.pt` — она локальная, в отличие от UTC `start_date`; адрес/город/район).
- **Страница события — `parse_next_data()`**: `<script id="__NEXT_DATA__">`,
  путь `props.pageProps.hydrationData.eventHydration.event` (title, detail, start/end
  ISO8601, `eventsAddress`, `onlineInfo`, `cancelled`). Только для `*/evento/*`.
- **Цена — `fetch_ticket_prices()`**: `GET event-page.svc.sympla.com.br/api/event-bff/
  purchase/event/{id}/tickets` → min `salePriceMonetary.decimal` среди видимых билетов.
  Цены нет в HTML, поэтому отдельный запрос; нет данных → `price/is_free=null`.
- `bileto.sympla.com.br/event/*` — другой хостинг, страница не парсится: факт целиком
  из search-данных (`run()` так и делает, `_merge_fill`).
- **HTML-фоллбэк** (`parse_html_fallback`) дозабирает пустое из OpenGraph/meta.
- Оффлайн self-test: `python3 sympla_agent.py --selftest` (на фикстурах реальных
  ответов; без сети/LLM) — держать зелёным.

## Что доделать
1. Подтвердить, что `publics=97,220` покрывает нужные детские/семейные категории
   (при необходимости добавить значения/города в `CONFIG_LISTING`).
2. При смене UA: ticket-API/`__NEXT_DATA__` могут отвечать иначе — следить за долей `null`.
3. Дедуп между ежедневными прогонами (накопление базы без повторов по `source_url`).

## Правила валидации (стадия 5)
- `source_url` отдаёт HTTP 200 (ловит выдумки/мёртвые ссылки).
- `start_date` парсится, в будущем, в допустимом дне недели (ловит «вторник без показа»).
- Обязательные поля на месте: title, start_date, source_url.
- Любая проблема → `status: "требует проверки"` + причина в `_issues`.

## Запуск и расписание
- Классификация: дешёвая модель (Haiku), уточнить актуальный id в docs.claude.com.
- Ключ: `ANTHROPIC_API_KEY` из окружения. **Не коммитить секреты.**
- Ежедневный прогон — через **Routines** (Claude Code на вебе), не отдельный cron.
- Облачная VM имеет интернет → можно живьём инспектировать Sympla при доработке discovery.

## Не ломать
- Оффлайн self-test (`python3 sympla_agent.py --selftest`) проходит — сохранять зелёным.
  Он же гоняется SessionStart-хуком (`.claude/hooks/session_start.sh`) на старте веб-сессии.
- Запуск на ограниченном списке без LLM/сети: `run(urls=[...], use_llm=False, check_url=False)`.
