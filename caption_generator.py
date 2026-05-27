"""
caption_generator.py — NVIDIA Llama (через NIM cascade) → JSON 5 слайдов для broker reel.

Принимает unit + project data → возвращает список из 5 slides (по типам hero/interior/view/roi/cta).
Использует cascade паттерн из nvidia_nim_cascade_playbook.md — 9 моделей, fallback по 429.

Никакого голоса в v1 — только captions/субтитры.
"""
from __future__ import annotations
import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY")
NIM_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

# Cascade моделей (см. nvidia_nim_cascade_playbook.md)
CASCADE_MODELS = [
    "nvidia/llama-3.3-nemotron-super-49b-v1",     # primary — best RU/JSON
    "meta/llama-3.3-70b-instruct",                # vendor-diff fallback
    "openai/gpt-oss-120b",                        # OpenAI-vendor fallback
    "mistralai/mixtral-8x22b-instruct-v0.1",
    "meta/llama-3.1-8b-instruct",                 # fast fallback
]

# Per-slug cooldown for 429
_COOLDOWNS: Dict[str, float] = {}


def _model_available(slug: str) -> bool:
    return _COOLDOWNS.get(slug, 0) <= time.time()


def _mark_429(slug: str, seconds: int = 60) -> None:
    _COOLDOWNS[slug] = time.time() + seconds


def call_nvidia_llama(messages: List[Dict], max_tokens: int = 1400, temperature: float = 0.4, prefer_json: bool = True) -> Optional[str]:
    """
    Call NVIDIA NIM cascade. Returns response content or None on full failure.

    prefer_json=True — пробуем сначала строгие-JSON модели (nemotron-49b, llama-3.3-70b),
    без рандомизации, чтоб не нарваться на gpt-oss-120b который любит обрезаться.
    """
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY not set in environment")

    if prefer_json:
        order = CASCADE_MODELS  # priority order: nemotron → llama-3.3-70b → ...
    else:
        # Randomize starting position to spread load (для bulk volume use case)
        start = random.randint(0, len(CASCADE_MODELS) - 1)
        order = CASCADE_MODELS[start:] + CASCADE_MODELS[:start]

    last_err = None
    for slug in order:
        if not _model_available(slug):
            continue
        try:
            payload = {
                "model": slug,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            # response_format поддерживается nemotron/llama-3.3, остальным игнорим
            if prefer_json and "nemotron" in slug or "llama-3.3" in slug:
                payload["response_format"] = {"type": "json_object"}
            r = requests.post(
                NIM_URL,
                headers={"Authorization": f"Bearer {NVIDIA_API_KEY}"},
                json=payload,
                timeout=20,
            )
            if r.status_code == 429:
                _mark_429(slug, 60)
                last_err = f"429 on {slug}"
                continue
            if r.status_code >= 500:
                last_err = f"{r.status_code} on {slug}"
                continue
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            print(f"  [llama] ✓ used {slug}", file=sys.stderr)
            return content
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
    print(f"  [llama] ✗ all models failed: {last_err}", file=sys.stderr)
    return None


# ───────────────────────────────────────────────────────────────────
# Промпт для генерации 5-кадрового сценария рилса
# ───────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Ты — sales-копирайтер рилсов для премиум-недвижимости в SEA (Таиланд, Бали).
Цель — продать. Каждый слайд должен дёргать конкретный sales-триггер: эксклюзив,
дефицит, выгода в цифрах, lifestyle, доказательство, контакт.

═════════════════ ИСТОЧНИКИ ФАКТУРЫ (по приоритету) ═════════════════
1. aiBrief проекта — это уже sales-brief от AI-парсера ("кому подходит, в чём сила,
   что проверить"). ДОСТАВАЙ ОТТУДА УГЛЫ: оператор, доходность, локация-фишка, target-buyer.
   Это первоисточник продающего тона.
2. unit.aiBrief — про юнит конкретно. Бери угол для INTERIOR slide.
3. description, infrastructure, distance_to_sea, beach_name, management_brand, developer
4. Только если ВСЁ выше пусто — generic, но без вранья.

═════════════════ СТИЛЬ ═════════════════
- Headline: 2-5 слов, макс 28 символов. Не клише, конкретные триггеры:
  ХОРОШО: "Operator: Unicorn", "7 мин до Найхарн", "Видовой пентхаус", "Окно в океан"
  ПЛОХО:  "Уникальная возможность", "Премиум-резиденция", "Сказочный вид"
- Подзаголовок: 1 строка, до 60 символов, с цифрами и конкретикой
- Тон премиум, без капса в середине предложения, без эмодзи

═════════════════ СЛАЙДЫ ═════════════════
- HERO: headline = название проекта (можно EN). sub = ГОРОД · СТРАНА (не более).
- INTERIOR: headline = ОДИН sales-угол про юнит ("1BR с лагуной", "Угловой пентхаус",
  "Этаж с видом", "Студия 32м²"). sub = "<area> · <BR> · <floor>".
- VIEW: headline = САМЫЙ СИЛЬНЫЙ location-hook ("7 мин до Найхарн", "На лагуне Layan",
  "Рядом Big Buddha"). Если distance_to_sea задан — ИСПОЛЬЗУЙ дословно.
  sub = ОДИН-ДВА доп. ориентира с расстоянием через " · ".
- ROI: НЕ ГЕНЕРЬ ТЕКСТ. Только числа из данных. Если поле пусто — оставь пустую строку.
- CTA: broker_name + contact + ОДНА фраза про проект (3-5 слов).

═════════════════ ЗАПРЕЩЕНО (anti-fabrication) ═════════════════
- НЕ ВЫДУМЫВАЙ ROI / installment / completion year / цену. Эти поля
  пост-обрабатываются на сервере из Firestore. Можешь оставить пустыми ("").
  Если выдумаешь — твоё значение будет ОТКЛОНЕНО.
- НЕ выдумывай гарантии доходности ("гарантированный ROI", "100% окупаемость").
- НЕ выдумывай скорость продаж ("осталось 3 юнита", если не указано в данных).
- НЕ переводи валюты — оставь как в данных (THB → ฿, RUB → ₽).
- Если данных нет — оставь поле "" (пустая строка), не "по запросу" и не "tba".

═════════════════ КРИТИЧНО ═════════════════
- НЕ копируй название проекта во всех слайдах.
- Headline в основном на русском (EN допустим только для названия проекта).
- Каждый слайд = ОДНО новое сообщение.

Output: строго валидный JSON. Без markdown fences, без комментариев."""

USER_PROMPT_TEMPLATE = """Данные проекта/юнита:
{unit_data}

Сгенерируй JSON с 5 слайдами для рилса 9:16. Структура:
{{
  "slides": [
    {{"type": "hero", "eyebrow": "<краткая категория, 1-2 слова caps>", "headline": "<название проекта>", "sub": "<локация>", "price": "<цена в формате 12,5 млн ₽>"}},
    {{"type": "interior", "eyebrow": "<INTERIOR or ПЛАН>", "headline": "<краткий headline о юните>", "sub": "<квадратура · комнаты · этаж>"}},
    {{"type": "view", "eyebrow": "<ЛОКАЦИЯ or ОКРУЖЕНИЕ>", "headline": "<что важно про расположение>", "sub": "<расстояние до ключевых точек>"}},
    {{"type": "roi", "eyebrow": "ЦИФРЫ", "roi_pct": "<например 9.1%>", "installment": "<рассрочка 0% · 2 года>", "completion": "<сдача 2028 или готово>"}},
    {{"type": "cta", "eyebrow": "СВЯЗАТЬСЯ", "broker_name": "<имя брокера>", "contact": "<@telegram_handle>", "project_name": "<краткое название проекта>"}}
  ]
}}

Возвращай ТОЛЬКО валидный JSON. Без markdown."""


def generate_reel_script(
    unit: Dict,
    project: Dict,
    broker: Dict,
) -> Optional[Dict]:
    """
    Генерирует 5-слайдовый сценарий рилса через NVIDIA Llama cascade.

    Inputs:
      unit: {price, area_sqm, bedrooms, floor, type, ...}
      project: {name, location, completion_year, roi_pct, installment_terms, ...}
      broker: {name, telegram_handle}

    Returns dict {slides: [...]} или None при полном падении cascade.
    """
    # Собираем краткое описание unit'a для промпта.
    # Приоритет: aiBrief → distance_to_sea → infrastructure → description → факты.
    unit_summary_lines = []
    name = project.get("name") or project.get("title") or "Проект"
    location = project.get("location") or project.get("city") or ""
    if name:
        unit_summary_lines.append(f"Проект: {name}")
    if project.get("subtitle"):
        unit_summary_lines.append(f"Подзаголовок: {project['subtitle']}")
    if location:
        unit_summary_lines.append(f"Локация: {location}")
    if project.get("country"):
        unit_summary_lines.append(f"Страна: {project['country']}")
    if project.get("completion_year"):
        unit_summary_lines.append(f"Сдача: {project['completion_year']}")

    # ────────────────── ГЛАВНЫЙ ИСТОЧНИК ФАКТУРЫ ──────────────────
    if project.get("aiBrief"):
        unit_summary_lines.append(f"AI-БРИФ (главное): {project['aiBrief'][:700]}")
    if project.get("description"):
        unit_summary_lines.append(f"Описание: {project['description'][:300]}")
    # ──────────────────────────────────────────────────────────────

    # Расстояния до моря/инфраструктуры — gold для VIEW слайда
    if project.get("distance_to_sea"):
        unit_summary_lines.append(f"До моря: {project['distance_to_sea']}")
    if project.get("beach_name"):
        beach_line = f"Ближайший пляж: {project['beach_name']}"
        if project.get("beach_distance_min"):
            beach_line += f" ({project['beach_distance_min']} мин)"
        unit_summary_lines.append(beach_line)

    # Инфраструктура — для INTERIOR/VIEW слайдов
    if project.get("infrastructure"):
        infra_items = [str(x) for x in project["infrastructure"][:6] if x]
        if infra_items:
            unit_summary_lines.append(f"Инфраструктура: {', '.join(infra_items)}")

    # Спец-акции — для HERO/CTA если есть
    if project.get("special_offers"):
        offers = [str(x) for x in project["special_offers"][:3] if x]
        if offers:
            unit_summary_lines.append(f"Спецпредложения: {' · '.join(offers)}")

    # Premium signals — оператор + застройщик
    if project.get("management_brand"):
        unit_summary_lines.append(f"Оператор управления: {project['management_brand']}")
    if project.get("developer"):
        unit_summary_lines.append(f"Застройщик: {project['developer']}")

    # ROI + Рассрочка
    if project.get("roi_pct"):
        unit_summary_lines.append(f"ROI: {project['roi_pct']}%")
    if project.get("installment_terms"):
        unit_summary_lines.append(f"Рассрочка: {project['installment_terms']}")
    if project.get("ownership_type"):
        unit_summary_lines.append(f"Тип владения: {project['ownership_type']}")

    # Unit-specific
    if unit.get("price_rub") or unit.get("price"):
        price_val = unit.get("price_rub") or unit.get("price")
        unit_summary_lines.append(f"Цена: {price_val}")
    if unit.get("area_sqm"):
        unit_summary_lines.append(f"Площадь: {unit['area_sqm']} м²")
    if unit.get("bedrooms"):
        unit_summary_lines.append(f"Спален: {unit['bedrooms']}")
    if unit.get("floor"):
        unit_summary_lines.append(f"Этаж: {unit['floor']}")
    if unit.get("type"):
        unit_summary_lines.append(f"Тип: {unit['type']}")
    if unit.get("view"):
        unit_summary_lines.append(f"Вид из окна: {unit['view']}")
    if unit.get("aiBrief"):
        unit_summary_lines.append(f"AI-БРИФ ЮНИТА: {unit['aiBrief'][:300]}")

    # Broker
    if broker.get("name"):
        unit_summary_lines.append(f"Брокер: {broker['name']}")
    if broker.get("telegram_handle"):
        unit_summary_lines.append(f"Контакт: {broker['telegram_handle']}")

    unit_data = "\n".join(unit_summary_lines)
    user_prompt = USER_PROMPT_TEMPLATE.format(unit_data=unit_data)

    raw = call_nvidia_llama(
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=1400,
        temperature=0.4,
        prefer_json=True,
    )

    if not raw:
        return None

    # Парсим JSON (модели иногда добавляют markdown fences)
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        data = json.loads(cleaned)
        if "slides" not in data or len(data["slides"]) != 5:
            print(f"  [llama] WARN: malformed slides count {len(data.get('slides', []))}", file=sys.stderr)
            return None
        # ANTI-FABRICATION: критические поля (цены, ROI, рассрочка, completion,
        # broker contact) — НЕ доверяем AI. Llama может вернуть "по запросу" или
        # выдумать ROI 12% — это юридически опасно для real-estate.
        # AI отвечает ТОЛЬКО за headlines/sub/eyebrow (стилистика).
        # Все цифры/факты overwrite из payload (Firestore source-of-truth).
        chip = _pick_hero_chip(project)
        highlight = _pick_roi_highlight(project)

        # Готовим source-of-truth значения (точно как в fallback_reel_script)
        price_str = unit.get("price_rub") or unit.get("price") or "по запросу"
        if isinstance(price_str, (int, float)) and price_str >= 1_000_000:
            price_str = f"{price_str / 1_000_000:.1f} млн ₽".replace(".", ",")
        completion = project.get("completion_year") or ""
        roi_val = project.get("roi_pct")
        roi_str = f"{roi_val}%" if roi_val else ""
        installment = project.get("installment_terms") or ""
        if installment in ("—", "-"):
            installment = ""
        broker_name_val = broker.get("name") or "Брокер"
        contact_val = broker.get("telegram_handle") or "@broker"
        project_name_val = project.get("name") or project.get("title") or "Проект"

        for sl in data["slides"]:
            t = sl.get("type")
            if t == "hero":
                # Цена — ВСЕГДА из CF. Llama не имеет права её менять.
                sl["price"] = price_str
                if chip:
                    sl["offer_chip"] = chip
            elif t == "roi":
                # ROI/installment/completion — ВСЕГДА из Firestore.
                sl["roi_pct"] = roi_str or "—"
                sl["installment"] = installment or "—"
                sl["completion"] = str(completion) if completion else "—"
                if highlight:
                    sl["highlight"] = highlight
            elif t == "cta":
                # Контакты брокера — точно как заданы. AI не выдумывает handle.
                sl["broker_name"] = broker_name_val
                sl["contact"] = contact_val
                sl["project_name"] = project_name_val
        return data
    except json.JSONDecodeError as e:
        print(f"  [llama] WARN: JSON parse failed: {e}\nRaw: {raw[:500]}", file=sys.stderr)
        return None


# ───────────────────────────────────────────────────────────────────
# Fallback: hardcoded scenario без AI (если NVIDIA cascade лег)
# ───────────────────────────────────────────────────────────────────
def _pick_hero_chip(project: Dict) -> Optional[str]:
    """Выбирает самый компактный закрывающий аргумент для chip на HERO слайде."""
    chip = project.get("hero_chip")
    if chip:
        return str(chip).strip()[:48]
    closing = project.get("sales_closing") or []
    if closing:
        # Берём самое короткое
        sorted_by_len = sorted([str(c) for c in closing if c], key=len)
        if sorted_by_len:
            return sorted_by_len[0][:48]
    return None


def _pick_roi_highlight(project: Dict) -> Optional[str]:
    """Highlight под ROI цифрами — приоритет: 2-й closing arg (не дублируем hero)."""
    closing = project.get("sales_closing") or []
    if len(closing) >= 2:
        return str(closing[1])[:60]
    if closing:
        return str(closing[0])[:60]
    return None


def fallback_reel_script(unit: Dict, project: Dict, broker: Dict) -> Dict:
    """
    Hardcoded fallback на случай если NVIDIA cascade полностью лег.
    Используем имеющиеся поля без AI-рерайта.
    Приоритет источников: distance_to_sea > beach_name > description > generic.
    """
    name = project.get("name") or project.get("title") or "Проект"
    location = project.get("location") or project.get("city") or ""

    price_str = unit.get("price_rub") or unit.get("price") or "по запросу"
    if isinstance(price_str, (int, float)):
        if price_str >= 1_000_000:
            price_str = f"{price_str / 1_000_000:.1f} млн ₽".replace(".", ",")

    area = f"{unit.get('area_sqm', '')} м²" if unit.get("area_sqm") else ""
    beds = f"{unit.get('bedrooms')} BR" if unit.get("bedrooms") else None
    floor = f"{unit.get('floor')} этаж" if unit.get("floor") else None
    completion = project.get("completion_year") or "tba"
    roi = project.get("roi_pct")
    roi_str = f"{roi}%" if roi else "—"
    installment = project.get("installment_terms") or "—"
    broker_name = broker.get("name") or "Брокер"
    contact = broker.get("telegram_handle") or "@broker"

    # Interior headline — пытаемся вытянуть угол из unit.type/view/aiBrief
    interior_headline = "Премиум-резиденция"
    if unit.get("type"):
        interior_headline = str(unit["type"])[:28]
    elif beds:
        interior_headline = f"{beds} резиденция"
    interior_sub_parts = [p for p in [area, beds, floor] if p]
    interior_sub = " · ".join(interior_sub_parts) if interior_sub_parts else "Премиум-планировка"

    # View headline — используем реальные данные
    view_headline = "Премиум-локация"
    view_sub_parts = []
    if project.get("distance_to_sea"):
        # aiBrief-стиль строка: "5 минут пешком" → headline
        view_headline = project["distance_to_sea"][:40]
    elif project.get("beach_distance_min") and project.get("beach_name"):
        view_headline = f"{project['beach_distance_min']} мин до {project['beach_name']}"[:40]
    elif project.get("beach_name"):
        view_headline = f"Рядом {project['beach_name']}"[:40]

    if project.get("beach_name"):
        beach_part = project["beach_name"]
        if project.get("beach_distance_min"):
            beach_part = f"{project['beach_name']} {project['beach_distance_min']} мин"
        view_sub_parts.append(beach_part)
    if project.get("infrastructure"):
        infra = [str(x) for x in project["infrastructure"][:2] if x]
        if infra:
            view_sub_parts.extend(infra)
    view_sub = " · ".join(view_sub_parts) if view_sub_parts else location

    chip = _pick_hero_chip(project)
    highlight = _pick_roi_highlight(project)

    return {
        "slides": [
            {
                "type": "hero",
                "eyebrow": (project.get("city") or "PRE-SALE").upper()[:14],
                "headline": name,
                "sub": location,
                "price": price_str,
                "offer_chip": chip,
            },
            {
                "type": "interior",
                "eyebrow": "ИНТЕРЬЕР",
                "headline": interior_headline,
                "sub": interior_sub,
            },
            {
                "type": "view",
                "eyebrow": "ЛОКАЦИЯ",
                "headline": view_headline,
                "sub": view_sub,
            },
            {
                "type": "roi",
                "eyebrow": "ЦИФРЫ",
                "roi_pct": roi_str,
                "installment": installment,
                "completion": str(completion),
                "highlight": highlight,
            },
            {
                "type": "cta",
                "eyebrow": "СВЯЗАТЬСЯ",
                "broker_name": broker_name,
                "contact": contact,
                "project_name": name,
            },
        ]
    }


# ───────────────────────────────────────────────────────────────────
# CLI smoke test
# ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Demo input matching Sun Hills Lakeside data
    demo_unit = {
        "price_rub": 12_500_000,
        "area_sqm": 65,
        "bedrooms": 2,
        "floor": "5/8",
        "type": "Apartment",
    }
    demo_project = {
        "name": "Sun Hills Lakeside",
        "location": "Layan · Phuket",
        "city": "Phuket",
        "country": "Thailand",
        "completion_year": 2028,
        "roi_pct": 9.1,
        "installment_terms": "0% · 2 года",
        "description": "Премиум-комплекс из 6 корпусов в Layan, 5 минут до пляжа Bang Tao, гостиничный оператор Unicorn Hospitality, фрихолд.",
    }
    demo_broker = {
        "name": "Любовь Стрельцова",
        "telegram_handle": "@ABG_MEDIA",
    }

    print("→ Generating reel script via NVIDIA cascade...")
    script = generate_reel_script(demo_unit, demo_project, demo_broker)

    if script is None:
        print("→ Cascade failed, using fallback")
        script = fallback_reel_script(demo_unit, demo_project, demo_broker)

    print(json.dumps(script, ensure_ascii=False, indent=2))
