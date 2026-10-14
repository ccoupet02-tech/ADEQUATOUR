"""Digest quotidien : tâches en retard / à venir par événement, envoyé par webhook ou e-mail."""
from __future__ import annotations

import logging
from datetime import date, timedelta

import requests

from .config import Settings
from .mailer import Mail
from .tasks import classify
from .util import fr_date

log = logging.getLogger("padel.notify")


def relevant_events(events: list, today: date, horizon: int, grace: int) -> list:
    return [e for e in events if e.date and today - timedelta(days=grace) <= e.date <= today + timedelta(days=horizon)]


def build_digest(settings: Settings, today: date, items: list[tuple]) -> str:
    """items = [(event, tasks)] ; ne liste que les événements avec du retard ou de l'imminent."""
    cfg = settings.get("digest", default={})
    soon, max_items = cfg.get("soon_days", 7), cfg.get("max_items", 10)
    blocks, total_late = [], 0
    for event, tasks in items:
        b = classify(tasks, event.date, today, settings.status("done"), soon)
        total_late += len(b["late"])
        if not (b["late"] or b["soon"]):
            continue
        d = (event.date - today).days
        when = f"J-{d}" if d > 0 else ("aujourd'hui" if d == 0 else f"J+{-d}")
        lines = [f"{event.name} - {fr_date(event.date)} ({when}) : {len(b['late'])} en retard · "
                 f"{len(b['soon'])} sous {soon} jours"]
        todo = [("⚠️", t, dl) for t, dl in b["late"]] + [("🔜", t, dl) for t, dl in b["soon"]]
        lines += [f"  {i} {t.name} (échéance {fr_date(dl)})" for i, t, dl in todo[:max_items]]
        if len(todo) > max_items:
            lines.append(f"  … et {len(todo) - max_items} autre(s)")
        blocks.append("\n".join(lines))
    head = f"{settings.brand} - point du {fr_date(today)}"
    if not blocks:
        return f"{head}\nRien d'urgent : aucune tâche en retard ni imminente. 🎉"
    return f"{head}\n{total_late} tâche(s) en retard au total\n\n" + "\n\n".join(blocks)


def send_webhook(url: str, text: str) -> None:
    r = requests.post(url, json={"text": text}, timeout=30)
    r.raise_for_status()


def deliver(settings: Settings, text: str, backend, webhook: bool = True) -> list[str]:
    """Webhook et/ou e-mail selon l'environnement. Retourne les canaux utilisés."""
    used = []
    hook = settings.env.get("NOTIFY_WEBHOOK_URL")
    if hook and webhook:
        send_webhook(hook, text)
        used.append("webhook")
    rcpts = [x.strip() for x in (settings.env.get("DIGEST_RECIPIENTS") or "").split(",") if x.strip()]
    if rcpts:
        backend.send(Mail(rcpts, f"{settings.brand} - digest des tâches", text))
        used.append("mail" if backend.real else "mail (.eml)")
    return used
