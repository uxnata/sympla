#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sympla kids/family events — единый скрипт для Google Colab.

Просто запусти этот файл в Colab (ячейка `%run sympla_colab.py` или кнопка ▶).
Он сам: поставит зависимости, возьмёт ANTHROPIC_API_KEY из Colab Secrets (🔑),
соберёт события São Paulo и др. через search-API Sympla, заполнит схему и
сохранит facts.json (LLM-классификация — опционально, при наличии ключа).

Правь блок CONFIG ниже: LIMIT и список городов/публику в CONFIG_LISTING.
Офлайн-проверка логики:  %run sympla_colab.py --selftest
"""

# --- bootstrap: поставить зависимости, если их нет (в Colab anthropic не предустановлен) ---
import subprocess as _sp, sys as _sys, importlib as _il
def _ensure(specs):
    for _mod, _spec in specs:
        try:
            _il.import_module(_mod)
        except ImportError:
            _sp.run([_sys.executable, "-m", "pip", "install", "-q", _spec], check=False)
_ensure([("requests", "requests>=2.31"), ("bs4", "beautifulsoup4>=4.12"),
         ("lxml", "lxml>=5.0"), ("anthropic", "anthropic>=0.40")])


def _load_api_key():
    """ANTHROPIC_API_KEY: из окружения -> Colab Secrets -> getpass. Без хардкода."""
    import os
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    try:
        from google.colab import userdata  # type: ignore
        k = userdata.get("ANTHROPIC_API_KEY")
        if k:
            os.environ["ANTHROPIC_API_KEY"] = k
            return k
    except Exception:
        pass
    try:
        import getpass
        k = getpass.getpass("ANTHROPIC_API_KEY (Enter — пропустить, LLM выключится): ").strip()
        if k:
            os.environ["ANTHROPIC_API_KEY"] = k
            return k
    except Exception:
        pass
    return None


# ======================= CONFIG для Colab — правь тут =======================
LIMIT = 100           # сколько событий собрать за прогон
# Города и публику правь в CONFIG_LISTING ниже (cities, publics).
# ===========================================================================



import json, re, time, html as _html
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timedelta
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------- 
# CONFIG
# ----------------------------------------------------------------------------- 
USER_AGENT = "FamilyEventsBot/0.1 (+contact@example.com)"  # представляться честно
REQUEST_DELAY_SEC = 1.5          # вежливый rate-limit между запросами
REQUEST_TIMEOUT = 20
SOON_WINDOW_HOURS = 48           # «скоро начнётся», если старт в пределах N часов
LLM_MODEL = "claude-haiku-4-5"   # дёшево для классификации; уточнить актуальный id в docs.claude.com
DEFAULT_LANG = "pt-BR"

# Discovery идёт через search-API discovery-bff (подтверждён живой инспекцией).
CONFIG_LISTING = {
    # Несколько городов одним прогоном (тот же детско-семейный фильтр publics).
    # Порядок = приоритет набора; дедуп по url между городами.
    "cities": ["São Paulo", "Rio de Janeiro", "Belo Horizonte", "Curitiba",
               "Porto Alegre", "Brasília", "Campinas"],
    "publics": "97,220",   # фильтр аудитории в discovery-bff = детские/семейные
    "limit": 24,           # размер страницы search-API
    "max_pages": 15,       # страховка от бесконечной пагинации на город
}

# Search-API афиши Sympla (discovery-bff поверх krakend). Найден живой инспекцией
# Network-таба: GET, отдаёт {"data":[{...}], "total":N, "limit":L, "page":P}.
# Каждый элемент несёт url/name/start_date_formats/location — этого хватает на
# базовый факт даже без захода на страницу события.
SEARCH_API = "https://www.sympla.com.br/api/discovery-bff/search/category-type"
_SEARCH_ONLY = ("name,start_date,end_date,images,event_type,duration_type,location,"
                "id,global_score,start_date_formats,end_date_formats,url,company,type,organizer")

# BFF-эндпоинт со списком билетов (цены). Найден живой инспекцией Network-таба:
# GET, отдаёт {"tickets":[...], "groups":[{"tickets":[...], "subgroups":[...]}]}.
# Цены нет в самой странице (__NEXT_DATA__) — она грузится отдельным запросом.
TICKETS_API = "https://event-page.svc.sympla.com.br/api/event-bff/purchase/event/{id}/tickets"

# Маппинг таксономии Sympla -> внутренние категории (расширять по мере встречи новых)
CATEGORY_MAP = {
    "infantil": "Детские события",
    "teatro": "Детские спектакли",
    "espetáculo": "Детские спектакли",
    "show": "Кино, шоу, концерты для детей",
    "cinema": "Кино, шоу, концерты для детей",
    "workshop": "Мастер-классы",
    "curso": "Мастер-классы",
    "passeio": "Прогулки и туры",
}


# ----------------------------------------------------------------------------- 
# Целевая схема
# ----------------------------------------------------------------------------- 
@dataclass
class Fact:
    title: Optional[str] = None
    description: Optional[str] = None
    start_date: Optional[str] = None   # YYYY-MM-DD
    start_time: Optional[str] = None   # HH:MM
    end_date: Optional[str] = None
    end_time: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    age_category: Optional[str] = None
    price: Optional[float] = None
    is_free: Optional[bool] = None
    source_url: Optional[str] = None
    category: Optional[str] = None
    format: Optional[str] = None        # offline | online
    language: Optional[str] = None
    suitable_for_children: Optional[bool] = None
    suitable_for_parents: Optional[bool] = None
    updated_at: Optional[str] = None
    status: Optional[str] = None
    _issues: list = field(default_factory=list)  # служебное: причины «требует проверки»


# ----------------------------------------------------------------------------- 
# Стадия 1 — discovery
# ----------------------------------------------------------------------------- 
def _cities() -> list[str]:
    return CONFIG_LISTING.get("cities") or [CONFIG_LISTING.get("city", "São Paulo")]


def _search_params(page: int, city: str) -> dict:
    return {
        "service": "/v4/search/query",
        "publics": CONFIG_LISTING["publics"],
        "only": _SEARCH_ONLY,
        "sort": "location-score",
        "type": "normal",
        "filter_sold_out": 1,
        "city": city,
        "location": city,
        "limit": CONFIG_LISTING["limit"],
        "page": page,
    }


def _search_page(page: int, city: str) -> dict:
    """Одна страница search-API по городу. Бросает requests/ValueError при ошибке."""
    time.sleep(REQUEST_DELAY_SEC)
    r = _session.get(SEARCH_API, params=_search_params(page, city), timeout=REQUEST_TIMEOUT,
                     headers={"Accept": "application/json",
                              "Referer": "https://www.sympla.com.br/"})
    r.raise_for_status()
    return r.json()


def search_events(limit: int = 100) -> list[dict]:
    """Сырые элементы событий из search-API по списку городов (дедуп по url)."""
    out: list[dict] = []
    seen: set[str] = set()
    max_pages = int(CONFIG_LISTING.get("max_pages", 15))
    page_size = int(CONFIG_LISTING.get("limit", 24))
    for city in _cities():
        if len(out) >= limit:
            break
        page = 1
        while len(out) < limit and page <= max_pages:
            try:
                data = _search_page(page, city)
            except (requests.RequestException, ValueError):
                break  # город недоступен — к следующему
            items = data.get("data") or []
            if not items:
                break
            for it in items:
                u = it.get("url")
                if u and u not in seen:
                    seen.add(u)
                    out.append(it)
            total = data.get("total")
            if total is not None and page * data.get("limit", page_size) >= total:
                break  # все результаты этого города выбраны
            page += 1
    return out[:limit]


def discover_event_urls(limit: int = 100) -> list[str]:
    """Список URL событий по городу через search-API (обёртка над search_events)."""
    return [it["url"] for it in search_events(limit) if it.get("url")]


# Месяцы pt-BR из start_date_formats.pt (там УЖЕ локальное время, в отличие от UTC start_date)
_PT_MONTHS = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
              "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}


def _parse_pt_datetime(s: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'Qui, 02 Jul - 2026 · 16:15' -> ('2026-07-02', '16:15'). Иначе (None, None)."""
    m = re.search(r"(\d{1,2})\s+([A-Za-zçÇ]{3})\s*-\s*(\d{4}).*?(\d{2}:\d{2})", s or "")
    if not m:
        return None, None
    day, mon, year, hhmm = m.groups()
    mm = _PT_MONTHS.get(mon[:3].lower())
    if not mm:
        return None, None
    return f"{year}-{mm:02d}-{int(day):02d}", hhmm


def fact_from_search(item: dict) -> Fact:
    """Базовый факт из элемента search-ответа (источник для bileto-событий и затравка
    для остальных). Время берём из *_date_formats.pt — оно локальное (UTC-3)."""
    f = Fact(source_url=item.get("url"))
    f.title = _clean(item.get("name"))
    f.start_date, f.start_time = _parse_pt_datetime((item.get("start_date_formats") or {}).get("pt"))
    f.end_date, f.end_time = _parse_pt_datetime((item.get("end_date_formats") or {}).get("pt"))

    loc = item.get("location") or {}
    if isinstance(loc, dict) and loc:
        street, num = loc.get("address"), loc.get("address_num")
        line = f"{street}, {num}" if street and num and str(num) != "0" else street
        city_state = " - ".join(p for p in [loc.get("city"), loc.get("state")] if p)
        parts = [loc.get("name"), line, loc.get("neighborhood"), city_state]
        f.address = ", ".join(p for p in parts if p) or None
        f.city = _clean(loc.get("city"))
        f.district = _clean(loc.get("neighborhood")) or None

    f.format = "online" if str(item.get("event_type", "")).upper() == "ONLINE" else "offline"
    return f


# ----------------------------------------------------------------------------- 
# Стадия 2 — fetch + parse (JSON-LD first)
# ----------------------------------------------------------------------------- 
_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt"})


def fetch_html(url: str) -> str:
    time.sleep(REQUEST_DELAY_SEC)
    r = _session.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text


def fetch_ticket_prices(event_id: str) -> Optional[float]:
    """Минимальная цена билета события из BFF-эндпоинта Sympla. None — если нет данных.

    Отдельный вежливый GET (цены нет в HTML). Любая сетевая/JSON-ошибка → None,
    чтобы не ронять прогон и не выдумывать цену.
    """
    url = TICKETS_API.format(id=event_id)
    try:
        time.sleep(REQUEST_DELAY_SEC)
        r = _session.get(url, timeout=REQUEST_TIMEOUT, headers={
            "Accept": "application/json",
            "Origin": "https://www.sympla.com.br",
            "Referer": "https://www.sympla.com.br/",
        })
        if r.status_code != 200:
            return None
        return _min_ticket_price(r.json())
    except (requests.RequestException, ValueError):
        return None


def extract_jsonld_events(html_text: str) -> list[dict]:
    """Вернуть все JSON-LD объекты типа Event со страницы (учёт @graph и массивов)."""
    soup = BeautifulSoup(html_text, "html.parser")
    found: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # некоторые сайты кладут невалидный JSON с комментариями/хвостами — пропускаем
            continue
        for obj in _iter_jsonld_objects(data):
            t = obj.get("@type", "")
            types = t if isinstance(t, list) else [t]
            if any(str(x).endswith("Event") for x in types):
                found.append(obj)
    return found


def _iter_jsonld_objects(data: Any):
    if isinstance(data, list):
        for x in data:
            yield from _iter_jsonld_objects(x)
    elif isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            for x in data["@graph"]:
                yield from _iter_jsonld_objects(x)
        else:
            yield data


def parse_event(jsonld: dict, source_url: str) -> Fact:
    """Сырое извлечение фактов из JSON-LD (без выдумок: чего нет — то None)."""
    f = Fact(source_url=source_url)
    f.title = _clean(jsonld.get("name"))
    f.description = _clean(jsonld.get("description"))

    sd, st = _split_dt(jsonld.get("startDate"))
    ed, et = _split_dt(jsonld.get("endDate"))
    f.start_date, f.start_time = sd, st
    f.end_date, f.end_time = ed, et

    loc = jsonld.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get("address") if isinstance(loc, dict) else None
    if isinstance(addr, dict):
        parts = [addr.get("streetAddress"), addr.get("addressLocality")]
        f.address = ", ".join(p for p in parts if p) or loc.get("name")
        f.city = addr.get("addressLocality")
        f.district = addr.get("addressRegion") or None
    elif isinstance(addr, str):
        f.address = addr
    elif isinstance(loc, dict):
        f.address = loc.get("name")

    # offers -> price/currency
    offers = jsonld.get("offers") or {}
    if isinstance(offers, list):
        prices = [_to_float(o.get("price")) for o in offers if isinstance(o, dict)]
        prices = [p for p in prices if p is not None]
        f.price = min(prices) if prices else None     # «от X»
    elif isinstance(offers, dict):
        f.price = _to_float(offers.get("price"))

    # формат из eventAttendanceMode, если есть
    mode = str(jsonld.get("eventAttendanceMode", "")).lower()
    if "online" in mode:
        f.format = "online"
    elif "offline" in mode or "inperson" in mode:
        f.format = "offline"

    # язык напрямую, если размечен
    if jsonld.get("inLanguage"):
        f.language = str(jsonld["inLanguage"])

    # eventStatus -> сразу пометим отмену для стадии normalize
    if str(jsonld.get("eventStatus", "")).endswith("EventCancelled"):
        f.status = "отменено"
    return f


# -----------------------------------------------------------------------------
# Стадия 2a — __NEXT_DATA__ (основной источник Sympla: сайт на Next.js, JSON-LD нет)
# -----------------------------------------------------------------------------
# Все факты события лежат в JSON, который страница отдаёт в
# <script id="__NEXT_DATA__">. Путь до события:
#   props.pageProps.hydrationData.eventHydration.event
# Это детерминированный разбор данных, которые сам сайт встроил в страницу —
# принцип «факты только из кода» соблюдён. Цены в объекте event нет (билеты
# грузятся отдельным запросом) → price/is_free оставляем None, не выдумываем.

def _next_data(html_text: str) -> Optional[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        return None
    raw = tag.string or tag.get_text() or ""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _event_from_next(data: dict) -> Optional[dict]:
    try:
        ev = data["props"]["pageProps"]["hydrationData"]["eventHydration"]["event"]
    except (KeyError, TypeError):
        return None
    return ev if isinstance(ev, dict) else None


def parse_next_data(html_text: str, source_url: str) -> Optional[Fact]:
    """Извлечь факт события из __NEXT_DATA__ Sympla. None — если структуры нет."""
    data = _next_data(html_text)
    if not data:
        return None
    ev = _event_from_next(data)
    if ev is None:
        return None

    f = Fact(source_url=source_url)
    f.title = _clean(ev.get("name"))

    # описание: strippedDetail (чистый текст) предпочтительнее detail (HTML)
    desc = ev.get("strippedDetail") or ev.get("detail")
    if desc and "<" in str(desc):
        desc = BeautifulSoup(str(desc), "html.parser").get_text(" ", strip=True)
    f.description = _clean(desc)

    # даты: ISO8601 из *MultiFormat надёжнее, иначе сырые startDate/endDate
    sd = (ev.get("startDateMultiFormat") or {}).get("ISO8601") or ev.get("startDate")
    ed = (ev.get("endDateMultiFormat") or {}).get("ISO8601") or ev.get("endDate")
    f.start_date, f.start_time = _split_dt(sd)
    f.end_date, f.end_time = _split_dt(ed)

    # адрес (структурированный)
    addr = ev.get("eventsAddress")
    if isinstance(addr, dict):
        street, num = addr.get("address"), addr.get("addressNum")
        line = f"{street}, {num}" if street and num else street
        city_state = " - ".join(p for p in [addr.get("city"), addr.get("state")] if p)
        parts = [addr.get("name"), line, addr.get("neighborhood"), city_state]
        f.address = ", ".join(p for p in parts if p) or None
        f.city = _clean(addr.get("city"))
        f.district = _clean(addr.get("neighborhood"))

    # формат: есть onlineInfo -> online, иначе offline
    f.format = "online" if ev.get("onlineInfo") else "offline"

    # сырьё категории Sympla -> попадёт в hay для _map_category
    cat = ev.get("eventsCategory")
    if isinstance(cat, dict):
        f.category = _clean(cat.get("description") or cat.get("name"))

    # отмена
    if ev.get("cancelled"):
        f.status = "отменено"
    return f


# -----------------------------------------------------------------------------
# Стадия 2b — HTML-фоллбэк (только то, что лежит в стандартных мета-тегах)
# -----------------------------------------------------------------------------
# Принцип фоллбэка: JSON-LD неполный? Дозабираем ТОЛЬКО надёжные, машинно-
# размеченные сигналы (OpenGraph/Twitter/meta + canonical).
# Даты/адрес/цену из произвольного текста НЕ выдумываем — пусть остаются None,
# тогда стадия 5 честно пометит запись «требует проверки». Это уважает принцип
# «факты приносит только детерминированный код, без догадок».
#
# ВАЖНО: бесплатность по слову «grátis» в тексте страницы НЕ определяем —
# это слово есть в навигации самого Sympla («Criar evento grátis») на каждой
# странице и ложно метило ВСЕ события бесплатными. is_free берём только из
# структурированных offers JSON-LD.
#
# TODO[live]: egress к sympla.com.br в этой среде закрыт политикой прокси (403),
# поэтому Sympla-специфичные CSS-селекторы карточки нельзя подтвердить вживую.
# Когда доступ появится — добавить сюда точечные селекторы даты/адреса/цены.


def parse_html_fallback(html_text: str, source_url: str, base: Optional[Fact] = None) -> Fact:
    """Дозаполнить факт из мета-тегов страницы. Возвращает (возможно тот же) Fact.

    Заполняет ТОЛЬКО пустые (None) поля base — JSON-LD всегда в приоритете.
    """
    f = base or Fact(source_url=source_url)
    soup = BeautifulSoup(html_text, "html.parser")

    def meta(*names_props) -> Optional[str]:
        for key, val in names_props:
            tag = soup.find("meta", attrs={key: val})
            if tag and tag.get("content"):
                return _clean(tag["content"])
        return None

    og_title = meta(("property", "og:title"), ("name", "twitter:title"))
    if not f.title:
        title_tag = soup.find("title")
        f.title = og_title or _clean(title_tag.get_text() if title_tag else None)

    if not f.description:
        f.description = meta(
            ("property", "og:description"),
            ("name", "twitter:description"),
            ("name", "description"),
        )

    # canonical как запасной source_url (исходный URL всё равно приоритетен)
    if not f.source_url:
        canon = soup.find("link", attrs={"rel": "canonical"})
        f.source_url = (canon.get("href") if canon else None) or meta(("property", "og:url"))

    return f


# -----------------------------------------------------------------------------
# Стадия 3 — enrich (LLM)  — классификация ПОВЕРХ добытого текста, без выдумок
# ----------------------------------------------------------------------------- 
ENRICH_SYSTEM = (
    "Ты классифицируешь событие для детского/семейного каталога. "
    "Используй ТОЛЬКО предоставленный текст (title, description, category). "
    "НЕ придумывай факты. Если данных недостаточно — ставь null. "
    "Верни СТРОГО JSON без пояснений и без markdown."
)
ENRICH_INSTRUCTION = """Поля для классификации:
- age_category: строка вроде "4+", "all", "10+" или null
- suitable_for_children: true/false/null
- suitable_for_parents: true/false/null
- language: BCP-47 ("pt-BR") или null
- format: "offline"|"online"|null
- category_internal: одна из [Детские спектакли, Кино, шоу, концерты для детей, Мастер-классы, Прогулки и туры, Детские события] или null
Верни только JSON-объект с этими ключами."""


def enrich_with_llm(f: Fact) -> dict:
    """Классификация недостающих полей. Ленивая загрузка SDK, чтобы оффлайн-тест не требовал ключа."""
    import anthropic  # noqa: локальный импорт намеренно
    client = anthropic.Anthropic()
    payload = {"title": f.title, "description": f.description, "category": f.category}
    resp = client.messages.create(
        model=LLM_MODEL,
        max_tokens=300,
        system=ENRICH_SYSTEM,
        messages=[{"role": "user",
                   "content": ENRICH_INSTRUCTION + "\n\nДанные:\n" + json.dumps(payload, ensure_ascii=False)}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return _safe_json(text)


# ----------------------------------------------------------------------------- 
# Стадия 4 — normalize
# ----------------------------------------------------------------------------- 
def normalize(f: Fact, enrich: dict, today: Optional[date] = None) -> Fact:
    today = today or date.today()

    # доразметка из LLM — только туда, где источник промолчал
    f.age_category = f.age_category or enrich.get("age_category")
    f.suitable_for_children = _first_not_none(f.suitable_for_children, enrich.get("suitable_for_children"))
    f.suitable_for_parents = _first_not_none(f.suitable_for_parents, enrich.get("suitable_for_parents"))
    f.language = f.language or enrich.get("language") or DEFAULT_LANG
    f.format = f.format or enrich.get("format") or "offline"
    f.category = f.category or enrich.get("category_internal") or _map_category(f)

    # is_free из цены
    if f.price is not None:
        f.is_free = (f.price == 0)

    # status (если не выставлен ранее как «отменено»)
    if f.status != "отменено":
        f.status = _derive_status(f, today)

    f.updated_at = datetime.now().replace(microsecond=0).isoformat()
    return f


def _derive_status(f: Fact, today: date) -> str:
    sd = _parse_date(f.start_date)
    ed = _parse_date(f.end_date) or sd
    if sd is None:
        return "требует проверки"
    if ed and ed < today:
        return "прошло"
    start_dt = _parse_dt(f.start_date, f.start_time)
    if start_dt and 0 <= (start_dt - datetime.now()).total_seconds() <= SOON_WINDOW_HOURS * 3600:
        return "скоро начнётся"
    return "новое"   # только что найдено; в очередной прогон станет «актуальное»


# ----------------------------------------------------------------------------- 
# Стадия 5 — validate
# ----------------------------------------------------------------------------- 
REQUIRED = ["title", "start_date", "source_url"]


def validate(f: Fact, check_url: bool = True) -> Fact:
    for key in REQUIRED:
        if not getattr(f, key):
            f._issues.append(f"missing:{key}")

    sd = _parse_date(f.start_date)
    if f.start_date and sd is None:
        f._issues.append("bad_start_date")
    # подозрительно далёкая дата (> 2 лет) — повод перепроверить
    if sd and sd > date.today() + timedelta(days=730):
        f._issues.append("date_too_far")

    if check_url and f.source_url:
        if not _url_ok(f.source_url):
            f._issues.append("url_unreachable")  # ловит выдумки/мёртвые ссылки

    if f._issues:
        f.status = "требует проверки"
    return f


def _url_ok(url: str) -> bool:
    try:
        time.sleep(REQUEST_DELAY_SEC)
        r = _session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True, stream=True)
        return r.status_code == 200
    except requests.RequestException:
        return False


# ----------------------------------------------------------------------------- 
# Orchestration
# ----------------------------------------------------------------------------- 
_IS_EVENT_PAGE = "sympla.com.br/evento/"   # только такие страницы парсятся (__NEXT_DATA__/цена)


def process_one(url: str, use_llm: bool = True, check_url: bool = True,
                fetch_prices: bool = True, base: Optional[Fact] = None) -> Fact:
    html_text = fetch_html(url)
    price = None
    if fetch_prices and _IS_EVENT_PAGE in (url or ""):   # цена живёт в отдельном BFF-эндпоинте
        eid = _event_id_from_url(url)
        if eid:
            price = fetch_ticket_prices(eid)
    return process_html(html_text, url, use_llm=use_llm, check_url=check_url, price=price, base=base)


def _merge_fill(f: Fact, base: Fact) -> None:
    """Заполнить пустые поля f данными из base (search-ответ). f всегда в приоритете."""
    for k in ("title", "description", "start_date", "start_time", "end_date", "end_time",
              "address", "city", "district", "format", "price", "is_free"):
        if getattr(f, k) is None and getattr(base, k) is not None:
            setattr(f, k, getattr(base, k))


def process_html(html_text: str, url: str, use_llm: bool = True, check_url: bool = True,
                 price: Optional[float] = None, base: Optional[Fact] = None) -> Fact:
    """Стадии 2..5 над уже скачанным HTML (вынесено ради оффлайн-тестов).

    Источники по приоритету: JSON-LD (schema.org) -> __NEXT_DATA__ (основной для
    Sympla) -> OpenGraph/meta-фоллбэк -> base из search-ответа дозабирает остаток.
    `price` (из ticket-эндпоинта) идёт в нормализацию -> is_free.
    """
    events = extract_jsonld_events(html_text)
    if events:
        f = parse_event(events[0], url)
    else:
        f = parse_next_data(html_text, url) or Fact(source_url=url)
    # HTML-фоллбэк дозабирает пустые поля из мета-тегов
    f = parse_html_fallback(html_text, url, base=f)
    if f.price is None and price is not None:   # цена из ticket-эндпоинта
        f.price = price
    # search-данные (base) — затравка/единственный источник для bileto-страниц
    if base is not None:
        _merge_fill(f, base)
        if base.status == "отменено":
            f.status = "отменено"
    if not f.title:
        # ни JSON-LD, ни __NEXT_DATA__, ни мета, ни search — нечего классифицировать
        f.status = "требует проверки"
        f._issues.append("no_data")
        return f
    f.category = _map_category(f)
    enrich = enrich_with_llm(f) if use_llm else {}
    f = normalize(f, enrich)
    f = validate(f, check_url=check_url)
    return f


def run(urls: Optional[list[str]] = None, limit: int = 100, out_path: str = "facts.json",
        use_llm: bool = True, check_url: bool = True, fetch_prices: bool = True) -> dict:
    # urls задан вручную -> элементы без search-данных; иначе тянем search (с базовым фактом)
    items = ([{"url": u} for u in urls[:limit]] if urls is not None
             else search_events(limit))
    facts: list[dict] = []
    for i, item in enumerate(items, 1):
        url = item.get("url")
        try:
            base = fact_from_search(item) if item.get("name") else None
            if url and _IS_EVENT_PAGE in url:
                f = process_one(url, use_llm=use_llm, check_url=check_url,
                                fetch_prices=fetch_prices, base=base)
            elif url:                       # bileto/прочее: страница не парсится -> только search
                f = process_html("", url, use_llm=use_llm, check_url=check_url, base=base)
            else:
                raise ValueError("no_url")
        except Exception as e:              # один битый URL не должен ронять прогон
            f = Fact(source_url=url, status="требует проверки")
            f._issues.append(f"error:{type(e).__name__}")
        rec = {k: v for k, v in asdict(f).items() if not k.startswith("_")}
        facts.append(rec)
        print(f"[{i}/{len(items)}] {f.status:16} {f.title or url}")
    clean = sum(1 for x in facts if x["status"] != "требует проверки")
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(facts, fp, ensure_ascii=False, indent=2)
    summary = {"total": len(facts), "clean": clean, "needs_review": len(facts) - clean, "out": out_path}
    print("SUMMARY:", summary)
    return summary


# ----------------------------------------------------------------------------- 
# helpers
# ----------------------------------------------------------------------------- 
def _clean(s):
    if not s:
        return None
    return _html.unescape(re.sub(r"\s+", " ", str(s))).strip() or None

def _split_dt(value):
    """'2026-06-14T15:00:00-03:00' -> ('2026-06-14','15:00'); '2026-06-14' -> (date, None)."""
    if not value:
        return None, None
    s = str(value)
    m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?", s)
    if not m:
        return None, None
    return m.group(1), m.group(2)

def _to_float(v):
    if v in (None, "", "0.00") and v != 0:
        # "0.00" — валидный ноль; разрулим ниже
        pass
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None

def _parse_date(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except (TypeError, ValueError):
        return None

def _parse_dt(d, t):
    if not d:
        return None
    try:
        return datetime.strptime(f"{d} {t or '00:00'}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None

def _map_category(f: Fact) -> Optional[str]:
    hay = " ".join(x for x in [f.category, f.title, f.description] if x).lower()
    for key, val in CATEGORY_MAP.items():
        if key in hay:
            return val
    return None

def _first_not_none(*vals):
    for v in vals:
        if v is not None:
            return v
    return None

def _safe_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}

def _event_id_from_url(url: str) -> Optional[str]:
    """Числовой id события из URL Sympla ('/evento/<slug>/3426870' или '<slug>__2222')."""
    m = re.search(r"(\d+)\D*$", url or "")
    return m.group(1) if m else None

def _collect_tickets(node) -> list[dict]:
    """Собрать все билеты из ответа BFF (tickets + groups[].tickets + subgroups[])."""
    out: list[dict] = []
    if isinstance(node, dict):
        out += [t for t in (node.get("tickets") or []) if isinstance(t, dict)]
        for key in ("groups", "subgroups"):
            for child in node.get(key) or []:
                out += _collect_tickets(child)
    return out

def _min_ticket_price(data: dict) -> Optional[float]:
    """Минимальная цена продажи среди видимых билетов. None — если цен нет."""
    prices = []
    for t in _collect_tickets(data):
        if t.get("show") is False:
            continue
        sp = t.get("salePriceMonetary") or {}
        p = sp.get("decimal")
        if isinstance(p, (int, float)):
            prices.append(float(p))
    return min(prices) if prices else None


# -----------------------------------------------------------------------------
# Оффлайн self-test — логика парсинга/нормализации/валидации без сети и без LLM.
# CLAUDE.md: «сохранять зелёным». Запуск:  python3 sympla_agent.py --selftest
# -----------------------------------------------------------------------------
_FIXTURE_JSONLD = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Event",
 "name":"Teatro Infantil: O Pequeno Príncipe",
 "description":"Espetáculo para a família, a partir de 4 anos.",
 "startDate":"2026-07-15T15:00:00-03:00","endDate":"2026-07-15T16:30:00-03:00",
 "eventAttendanceMode":"https://schema.org/OfflineEventAttendanceMode",
 "eventStatus":"https://schema.org/EventScheduled",
 "location":{"@type":"Place","name":"Teatro Paulo Autran",
   "address":{"@type":"PostalAddress","streetAddress":"Praca Roosevelt, 210","addressLocality":"Sao Paulo","addressRegion":"SP"}},
 "offers":{"@type":"Offer","price":"40.00","priceCurrency":"BRL"},
 "inLanguage":"pt-BR"}
</script></head><body></body></html>"""

# JSON-LD без описания/цены — проверяем, что HTML-фоллбэк дозабирает мета + «Gratuito»
_FIXTURE_PARTIAL = """<html><head>
<meta property="og:title" content="Oficina de Desenho para Crianças">
<meta property="og:description" content="Workshop gratuito de desenho, 6+">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Event",
 "name":"Oficina de Desenho para Crianças",
 "startDate":"2026-08-01","location":{"@type":"Place","name":"SESC"}}
</script></head><body><p>Entrada: Gratuito. Vagas limitadas.</p></body></html>"""

# Элемент search-ответа discovery-bff (форма с живого API) — bileto-событие:
# страница не парсится, факт собирается прямо из search-данных.
_FIXTURE_SEARCH_ITEM = {
    "url": "https://bileto.sympla.com.br/event/66711",
    "name": "Chapeuzinho Vermelho e o Lobo ",
    "event_type": "NORMAL",
    "start_date": "2026-06-21T19:15:00+00:00",   # UTC — НЕ берём напрямую
    "start_date_formats": {"pt": "Dom, 21 Jun - 2026 · 16:15"},  # локальное время
    "end_date_formats": {"pt": "Dom, 26 Jul - 2026 · 16:15"},
    "location": {"name": "Teatro Bibi Ferreira", "address": "Av. Brigadeiro Luiz Antônio, 931",
                 "address_num": "0", "neighborhood": "", "city": "São Paulo", "state": "SP"},
}

# Sympla-страница: JSON-LD нет, всё в __NEXT_DATA__ (как на реальном сайте)
_FIXTURE_NEXT = """<html><head>
<script id="__NEXT_DATA__" type="application/json">
{"props":{"pageProps":{"hydrationData":{"eventHydration":{"event":{
 "name":"Colônia de Férias | Clube Regatas | Inverno 2026",
 "strippedDetail":"Para crianças de 5 a 12 anos. Muita diversão e natureza.",
 "startDate":"2026-07-06 08:00:00",
 "startDateMultiFormat":{"ISO8601":"2026-07-06T08:00:00-03:00"},
 "endDateMultiFormat":{"ISO8601":"2026-07-17T17:00:00-03:00"},
 "eventsAddress":{"name":"Clube Campineiro de Regatas","address":"Av. Coronel Silva Telles","addressNum":"462","neighborhood":"Cambuí","city":"Campinas","state":"SP"},
 "onlineInfo":null,
 "eventsCategory":{"name":"infantil","description":"Infantil"},
 "cancelled":false}}}}}}
</script></head><body></body></html>"""


def selftest() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    today = date(2026, 6, 29)

    # 1) JSON-LD путь
    evs = extract_jsonld_events(_FIXTURE_JSONLD)
    check(len(evs) == 1, "jsonld: ожидался 1 Event")
    f = process_html(_FIXTURE_JSONLD, "https://www.sympla.com.br/evento/x/1",
                     use_llm=False, check_url=False)
    check(f.title == "Teatro Infantil: O Pequeno Príncipe", f"title={f.title!r}")
    check(f.start_date == "2026-07-15" and f.start_time == "15:00", f"dt={f.start_date} {f.start_time}")
    check(f.price == 40.0 and f.is_free is False, f"price={f.price} free={f.is_free}")
    check(f.format == "offline", f"format={f.format}")
    check(f.language == "pt-BR", f"lang={f.language}")
    check(f.category == "Детские события", f"cat={f.category}")
    check(f.status != "требует проверки" and not f._issues, f"status={f.status} issues={f._issues}")

    # 2) HTML-фоллбэк дозабирает описание из og, но НЕ выдумывает бесплатность из
    #    текста: слово «Gratuito» в теле страницы (или в навигации Sympla) НЕ должно
    #    помечать событие бесплатным — is_free остаётся None без offers JSON-LD.
    f2 = process_html(_FIXTURE_PARTIAL, "https://www.sympla.com.br/oficina__2222",
                      use_llm=False, check_url=False)
    check(f2.description == "Workshop gratuito de desenho, 6+", f"desc={f2.description!r}")
    check(f2.is_free is None and f2.price is None, f"free={f2.is_free} price={f2.price}")
    check(f2.category == "Мастер-классы", f"cat2={f2.category}")

    # 3) глухая страница без JSON-LD/__NEXT_DATA__/мета -> требует проверки
    f3 = process_html("<html><body>nada</body></html>", "https://x/y",
                      use_llm=False, check_url=False)
    check(f3.status == "требует проверки" and "no_data" in f3._issues, f"empty={f3.status} {f3._issues}")

    # 3b) Sympla __NEXT_DATA__ путь: дата+адрес берутся из встроенного JSON
    fn = process_html(_FIXTURE_NEXT, "https://www.sympla.com.br/evento/colonia/3426870",
                      use_llm=False, check_url=False)
    check(fn.start_date == "2026-07-06" and fn.start_time == "08:00", f"next dt={fn.start_date} {fn.start_time}")
    check(fn.end_date == "2026-07-17", f"next end={fn.end_date}")
    check(fn.city == "Campinas" and fn.district == "Cambuí", f"next city/distr={fn.city}/{fn.district}")
    check(fn.address and "Av. Coronel Silva Telles, 462" in fn.address, f"next addr={fn.address}")
    check(fn.format == "offline", f"next format={fn.format}")
    check(fn.category == "Детские события", f"next cat={fn.category}")
    check("missing:start_date" not in fn._issues, f"next issues={fn._issues}")

    # 3c) отменённое событие из __NEXT_DATA__
    fc = process_html(_FIXTURE_NEXT.replace('"cancelled":false', '"cancelled":true'),
                      "https://www.sympla.com.br/evento/colonia/3426870",
                      use_llm=False, check_url=False)
    check(fc.status == "отменено", f"next cancelled={fc.status}")

    # 3d) парсер цен ticket-эндпоинта: min среди видимых билетов (форма реального ответа)
    ticket_json = {"tickets": [], "groups": [{"groupId": "1", "tickets": [
        {"salePriceMonetary": {"decimal": 287, "integer": 28700}, "isFree": False, "show": True},
        {"salePriceMonetary": {"decimal": 150.5, "integer": 15050}, "show": True},
        {"salePriceMonetary": {"decimal": 10, "integer": 1000}, "show": False},  # скрытый — игнор
    ], "subgroups": [{"tickets": [
        {"salePriceMonetary": {"decimal": 99.9, "integer": 9990}, "show": True},
    ]}]}]}
    check(_min_ticket_price(ticket_json) == 99.9, f"minprice={_min_ticket_price(ticket_json)}")
    check(_min_ticket_price({"tickets": [], "groups": []}) is None, "minprice empty -> None")

    # 3e) цена из ticket-эндпоинта пробрасывается в нормализацию -> is_free
    fp = process_html(_FIXTURE_NEXT, "https://www.sympla.com.br/evento/colonia/3426870",
                      use_llm=False, check_url=False, price=287.0)
    check(fp.price == 287.0 and fp.is_free is False, f"price inject={fp.price}/{fp.is_free}")
    ff = process_html(_FIXTURE_NEXT, "https://www.sympla.com.br/evento/colonia/3426870",
                      use_llm=False, check_url=False, price=0.0)
    check(ff.is_free is True, f"free inject={ff.is_free}")

    # 3f) event id из URL
    check(_event_id_from_url("https://www.sympla.com.br/evento/x/3426870") == "3426870", "eid1")
    check(_event_id_from_url("https://www.sympla.com.br/teatro-infantil__2222") == "2222", "eid2")

    # 4) search-API: факт из элемента ответа (локальное время из *_formats.pt, адрес)
    check(_parse_pt_datetime("Dom, 21 Jun - 2026 · 16:15") == ("2026-06-21", "16:15"),
          f"pt_dt={_parse_pt_datetime('Dom, 21 Jun - 2026 · 16:15')}")
    fs = fact_from_search(_FIXTURE_SEARCH_ITEM)
    check(fs.title == "Chapeuzinho Vermelho e o Lobo", f"search title={fs.title!r}")
    check(fs.start_date == "2026-06-21" and fs.start_time == "16:15", f"search dt={fs.start_date} {fs.start_time}")
    check(fs.city == "São Paulo" and fs.district is None, f"search city/distr={fs.city}/{fs.district}")
    check(fs.address == "Teatro Bibi Ferreira, Av. Brigadeiro Luiz Antônio, 931, São Paulo - SP",
          f"search addr={fs.address}")  # address_num '0' опущен
    # 4b) bileto-событие через run-путь: страница не парсится -> факт целиком из base
    fb = process_html("", _FIXTURE_SEARCH_ITEM["url"], use_llm=False, check_url=False, base=fs)
    check(fb.title == "Chapeuzinho Vermelho e o Lobo" and fb.start_date == "2026-06-21",
          f"bileto merge={fb.title!r}/{fb.start_date}")
    check("missing:start_date" not in fb._issues, f"bileto issues={fb._issues}")

    # 5) статусы: прошло / скоро / отменено / нет даты
    past = normalize(Fact(title="t", source_url="u", start_date="2020-01-01"), {}, today=today)
    check(past.status == "прошло", f"past={past.status}")
    nodate = validate(normalize(Fact(title="t", source_url="u"), {}, today=today), check_url=False)
    check(nodate.status == "требует проверки", f"nodate={nodate.status}")
    cancelled = Fact(title="t", source_url="u", start_date="2026-07-15", status="отменено")
    cancelled = normalize(cancelled, {}, today=today)
    check(cancelled.status == "отменено", f"cancelled={cancelled.status}")

    # 6) валидация ловит битую дату и слишком далёкую
    bad = validate(Fact(title="t", source_url="u", start_date="2026-13-40"), check_url=False)
    check("bad_start_date" in bad._issues, f"baddate={bad._issues}")

    if fails:
        print("SELFTEST: FAIL")
        for m in fails:
            print("  -", m)
        return 1
    print("SELFTEST: OK (все группы проверок пройдены)")
    return 0


# ----------------------------------------------------------------------------- 
# Точка входа для Colab
# ----------------------------------------------------------------------------- 
def _colab_main():
    key = _load_api_key()
    use_llm = bool(key)
    print("LLM-классификация:", "ВКЛ (Haiku)" if use_llm else "ВЫКЛ — нет ANTHROPIC_API_KEY")
    print(f"Города: {CONFIG_LISTING['cities']} | publics: {CONFIG_LISTING['publics']}")
    print("Сбор событий через search-API Sympla (discovery-bff)...")
    summary = run(limit=LIMIT, use_llm=use_llm, check_url=True)  # сам ходит в search-API
    print("\nГотово. Файл:", summary.get("out"))
    return summary


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv or "--test" in sys.argv:
        raise SystemExit(selftest())
    _colab_main()
