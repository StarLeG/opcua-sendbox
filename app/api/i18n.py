"""Многоязычность UI: русский (дефолт) и английский.

Язык выбирается по `?lang=` либо cookie `lang`; страницы рендерятся на одном
языке целиком. Строки интерфейса задаются парами рядом с местом использования
(`t(lang, "RU", "EN")`), переводы строк JS-модулей живут в `static/i18n.js`.
"""
from __future__ import annotations

from typing import Literal

from fastapi import Request

Lang = Literal["ru", "en"]

#: Язык без явного выбора.
DEFAULT_LANG: Lang = "ru"
COOKIE_NAME = "lang"


def resolve_lang(request: Request) -> Lang:
    """Язык ответа: query `?lang=` важнее cookie, иначе русский."""
    raw = request.query_params.get("lang") or request.cookies.get(COOKIE_NAME) or ""
    return "en" if raw.strip().lower().startswith("en") else "ru"


def t(lang: Lang, ru: str, en: str) -> str:
    """Выбрать строку интерфейса по языку."""
    return ru if lang == "ru" else en


def lang_links(lang: Lang, path: str) -> str:
    """Переключатель языка: две ссылки, активная помечена классом."""
    other: Lang = "en" if lang == "ru" else "ru"
    return (
        f'<a class="lang{" active" if lang == "ru" else ""}" '
        f'href="{path}?lang=ru">RU</a>'
        f'<a class="lang{" active" if lang == "en" else ""}" '
        f'href="{path}?lang=en">EN</a>'
        f'<span class="lang-keep" hidden data-lang="{other}"></span>'
    )
