"""Événements : lecture, recherche et création complète (page + staff + tâches)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from .config import Settings
from .notion import callout, heading, paragraph
from .tasks import ensure_event_tasks
from .util import fr_date, norm, norm_id, parse_date

log = logging.getLogger("padel.events")

SUMMARY_MARKER = "SYNTHÈSE AUTO"


@dataclass
class Event:
    id: str
    name: str
    date: date | None
    venue: str = ""
    url: str = ""


def event_from_page(settings: Settings, page: dict) -> Event:
    s = settings.schema("events")
    return Event(id=page["id"], name=(s.read(page, "name") or "").strip(),
                 date=parse_date(s.read(page, "date")), venue=s.read(page, "venue") or "",
                 url=page.get("url", ""))


def list_events(notion, settings: Settings, since: date | None = None) -> list[Event]:
    s = settings.schema("events")
    flt = s.filter("date", "on_or_after", since.isoformat()) if since else None
    events = [event_from_page(settings, p) for p in notion.query_database(settings.events_db, flt)]
    events = [e for e in events if e.name]
    return sorted(events, key=lambda e: (e.date is None, e.date or date.max, e.name))


def find_event(events: list[Event], ident: str) -> Event:
    """Par identifiant de page, nom exact (sans accent/casse) ou préfixe unique."""
    n = norm(ident)
    by_id = [e for e in events if norm_id(e.id) == norm_id(ident)]
    if by_id:
        return by_id[0]
    exact = [e for e in events if norm(e.name) == n]
    if exact:
        return exact[0]
    prefix = [e for e in events if norm(e.name).startswith(n)]
    if len(prefix) == 1:
        return prefix[0]
    names = ", ".join(e.name for e in events)
    raise LookupError(f"Événement '{ident}' introuvable ou ambigu. Disponibles : {names}")


def staff_db_properties(settings: Settings) -> dict:
    s = settings.schema("staff")
    yes_no = {"select": {"options": [{"name": "Oui", "color": "green"}, {"name": "Non", "color": "pink"}]}}
    return {
        s.prop("name"): {"title": {}},
        s.prop("prenom"): {"rich_text": {}},
        s.prop("role"): {"rich_text": {}},
        s.prop("shirt"): {"select": {"options": [{"name": n} for n in ("XS", "S", "M", "L", "XL", "XXL")]}},
        s.prop("hotel"): yes_no,
        s.prop("transport"): {"select": {"options": [{"name": "Voiture"}, {"name": "Train"}]}},
        s.prop("meal"): yes_no,
    }


def _todo(value) -> str:
    return "à renseigner" if value in (None, "") else str(value)


def event_page_blocks(price=None, contact_name=None, contact_phone=None, contact_email=None,
                      courts_indoor=None, courts_outdoor=None, workshop_room=None) -> list[dict]:
    contact = " · ".join(x for x in (contact_name, contact_phone, contact_email) if x) or "à renseigner"
    lines = [
        f"Contact sur place : {contact}",
        f"Prix : {_todo(price)}",
        f"Terrains indoor : {_todo(courts_indoor)}",
        f"Terrains outdoor : {_todo(courts_outdoor)}",
        f"Salle ateliers : {_todo(workshop_room)}",
    ]
    return [
        heading("📋 Caractéristiques de l'événement"),
        callout("\n".join(lines), "📍"),
        heading("📄 Devis"),
        paragraph("Glisser le PDF du devis ici."),
        heading("🤖 Synthèse automatique"),
        callout(f"{SUMMARY_MARKER} — en attente de la première mise à jour "
                "(python -m padel refresh).", "🤖"),
        heading("👨‍💼 Staff"),
    ]


def create_event(notion, settings: Settings, name: str, event_date: date, *, venue=None, dr=None,
                 price=None, contact_name=None, contact_phone=None, contact_email=None,
                 courts_indoor=None, courts_outdoor=None, workshop_room=None, with_tasks: bool = True,
                 existing: list[Event] | None = None) -> tuple[Event, list[str]]:
    """Crée la page événement, sa base Staff et ses tâches. Refuse un nom déjà utilisé."""
    existing = existing if existing is not None else list_events(notion, settings)
    if any(norm(e.name) == norm(name) for e in existing):
        raise ValueError(f"L'événement « {name} » existe déjà (aucune création effectuée).")
    s = settings.schema("events")
    # prix, contact, terrains, salle : écrits dans le bloc « Caractéristiques » de la page
    # (la base Global view n'a pas ces colonnes)
    props = s.build({"name": name, "date": event_date, "venue": venue, "dr": dr})
    blocks = event_page_blocks(price, contact_name, contact_phone, contact_email,
                               courts_indoor, courts_outdoor, workshop_room)
    page = notion.create_page({"database_id": settings.events_db}, props, blocks)
    event = Event(id=page["id"], name=name, date=event_date, venue=venue or "", url=page.get("url", ""))
    notion.create_database(event.id, f"Staff {name}", staff_db_properties(settings))
    created = ensure_event_tasks(notion, settings, event, existing=[]) if with_tasks else []
    log.info("Événement %s créé (%s) : %d tâches", name, fr_date(event_date), len(created))
    return event, created
