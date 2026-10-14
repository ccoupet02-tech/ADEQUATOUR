"""Synthèse automatique écrite dans un bloc 'callout' de la page de chaque événement."""
from __future__ import annotations

from collections import Counter
from datetime import date

from .config import Settings
from .events import SUMMARY_MARKER, Event
from .notion import block_text, callout, heading, rich
from .tasks import Task, classify
from .util import fr_date


def _countdown(event_date: date | None, today: date) -> str:
    if event_date is None:
        return "date à confirmer"
    d = (event_date - today).days
    return "aujourd'hui" if d == 0 else (f"J-{d}" if d > 0 else f"J+{-d}")


def build_summary(settings: Settings, event: Event, tasks: list[Task], participants: list,
                  today: date, soon_days: int = 7, max_items: int = 8) -> str:
    buckets = classify(tasks, event.date, today, settings.status("done"), soon_days)
    total = sum(len(buckets[k]) for k in buckets)
    done = len(buckets["done"])
    pct = round(100 * done / total) if total else 0
    kinds = Counter((p.type or "sans type") for p in participants)
    accepted = sum(1 for p in participants if p.invite_status == settings.get("invite_statuses", "accepted"))
    lines = [
        f"{SUMMARY_MARKER} — mise à jour le {fr_date(today)}",
        f"📅 {event.name} · {fr_date(event.date)} · {_countdown(event.date, today)}",
        f"👥 Participants : {len(participants)} fiches · {accepted} acceptés"
        + (" · " + " · ".join(f"{k} {v}" for k, v in sorted(kinds.items())) if participants else ""),
        f"✅ Tâches : {done}/{total} faites ({pct} %) · ⚠️ {len(buckets['late'])} en retard · "
        f"🔜 {len(buckets['soon'])} sous {soon_days} jours",
    ]
    todo = [("⚠️", t, d) for t, d in buckets["late"]] + [("🔜", t, d) for t, d in buckets["soon"]]
    if todo:
        lines.append("À faire en priorité :")
        for icon, t, d in todo[:max_items]:
            lines.append(f"• {icon} {t.name} — échéance {fr_date(d)}")
        if len(todo) > max_items:
            lines.append(f"… et {len(todo) - max_items} autre(s)")
    return "\n".join(lines)


def refresh_summary(notion, settings: Settings, event: Event, tasks: list[Task], participants: list,
                    today: date) -> str:
    text = build_summary(settings, event, tasks, participants, today,
                         soon_days=settings.get("digest", "soon_days", default=7))
    for block in notion.list_children(event.id):
        if block.get("type") == "callout" and SUMMARY_MARKER in block_text(block):
            notion.update_block(block["id"], {"callout": {"rich_text": rich(text)}})
            return text
    notion.append_children(event.id, [heading("🤖 Synthèse automatique"), callout(text, "🤖")])
    return text
