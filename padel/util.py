"""Petites fonctions utilitaires (normalisation de texte, dates, identifiants)."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime


def norm(value) -> str:
    """Minuscule, sans accents, apostrophes unifiées, espaces compactés."""
    if value is None:
        return ""
    s = str(value).replace("’", "'").replace("‘", "'")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def tokens(value) -> list[str]:
    return re.findall(r"[a-z0-9]+", norm(value))


_SUFFIX = re.compile(r"\s*\(\d+\)\s*$")


def base_task_name(name: str | None) -> str:
    """'Invitation (1)' -> 'Invitation' (suffixe créé par la duplication Notion)."""
    return _SUFFIX.sub("", (name or "").strip()).strip()


def norm_id(value: str | None) -> str:
    return (value or "").replace("-", "").lower()


def parse_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def fr_date(d: date | None) -> str:
    return d.strftime("%d/%m/%Y") if d else "date à confirmer"


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(value)).strip("-") or "x"
