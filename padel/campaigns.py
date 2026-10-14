"""Campagnes d'e-mails : invitation, relance, récap KAM, retour d'événement.
Rien n'est envoyé sans --send ; après un envoi réel, statuts et tâches associés sont mis à jour."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .config import Settings
from .mailer import Mail, render
from .participants import Participant, is_invited
from .tasks import fetch_tasks, mark_task
from .util import fr_date, norm

log = logging.getLogger("padel.campaigns")


@dataclass
class CampaignResult:
    kind: str
    sent: int = 0
    skipped: list[str] = field(default_factory=list)
    real: bool = False

    def __str__(self) -> str:
        mode = "ENVOYÉS" if self.real else "écrits en .eml (aucun envoi)"
        s = f"{self.kind} : {self.sent} mail(s) {mode}"
        return s + "".join(f"\n  - ignoré : {x}" for x in self.skipped)


def event_ctx(settings: Settings, event) -> dict:
    return {"name": event.name, "date_fr": fr_date(event.date), "venue": event.venue,
            "register_url": settings.typeform_link(event.name),
            "photos_url": settings.get("mail", "photos_url", default=""),
            "feedback_url": settings.get("mail", "feedback_url", default="")}


def _base(settings: Settings, event) -> dict:
    return {"brand": settings.brand, "event": event_ctx(settings, event),
            "signature": settings.get("mail", "signature", default=settings.brand)}


def _finish(notion, settings, event, backend, result: CampaignResult, task_key: str | None,
            update_status: bool) -> CampaignResult:
    result.real = backend.real
    if backend.real and update_status and task_key and result.sent:
        mark_task(notion, settings, event, task_key, settings.status("done"),
                  fetch_tasks(notion, settings, event.id))
    return result


def invitation(notion, settings: Settings, event, participants: list[Participant], backend,
               update_status: bool = True) -> CampaignResult:
    """Invite les fiches avec e-mail et pas encore invitées ; pose « Pas de réponse » après envoi réel."""
    res = CampaignResult("invitation")
    ps = settings.schema("participants")
    for p in participants:
        if is_invited(p, settings):
            continue
        if not p.email:
            res.skipped.append(f"{p.full_name or p.id} : pas d'e-mail")
            continue
        subject, body = render("invitation", p=p, **_base(settings, event))
        backend.send(Mail([p.email], subject, body))
        res.sent += 1
        if backend.real:
            notion.update_page(p.id, ps.build({"invite_status": settings.get("invite_statuses", "invited")}))
    return _finish(notion, settings, event, backend, res, "invitation", update_status)


def relance(notion, settings: Settings, event, participants: list[Participant], backend,
            update_status: bool = True) -> CampaignResult:
    res = CampaignResult("relance")
    wanted = {norm(s) for s in settings.get("mail", "relance_statuses", default=["Pas de réponse"])}
    for p in participants:
        if norm(p.invite_status) not in wanted:
            continue
        if not p.email:
            res.skipped.append(f"{p.full_name or p.id} : pas d'e-mail")
            continue
        subject, body = render("relance", p=p, **_base(settings, event))
        backend.send(Mail([p.email], subject, body))
        res.sent += 1
    return _finish(notion, settings, event, backend, res, "relance", update_status)


def recap_kam(notion, settings: Settings, event, participants: list[Participant], backend,
              update_status: bool = True) -> CampaignResult:
    res = CampaignResult("recap-kam")
    to = list(settings.get("mail", "kam_recipients", default=[]) or [])
    if not to:
        res.skipped.append("aucun destinataire (mail.kam_recipients dans config.yaml)")
        return res
    accepted = settings.get("invite_statuses", "accepted")
    lines = [f"{p.full_name or p.email} ({p.company or 'entreprise ?'}) - {p.type or 'type ?'} - "
             f"{p.invite_status or 'non invité'}"
             for p in sorted(participants, key=lambda p: (norm(p.company), norm(p.full_name)))]
    subject, body = render("recap_kam", lines=lines, total=len(participants),
                           accepted=sum(p.invite_status == accepted for p in participants),
                           **_base(settings, event))
    backend.send(Mail(to, subject, body))
    res.sent = 1
    return _finish(notion, settings, event, backend, res, "recap_kam", update_status)


def retour(notion, settings: Settings, event, participants: list[Participant], backend,
           update_status: bool = True) -> CampaignResult:
    res = CampaignResult("retour")
    excluded = {norm(x) for x in settings.get("tournament", "exclude_invite_status", default=[])}
    for p in participants:
        if norm(p.invite_status) in excluded:
            continue
        if not p.email:
            res.skipped.append(f"{p.full_name or p.id} : pas d'e-mail")
            continue
        subject, body = render("retour", p=p, **_base(settings, event))
        backend.send(Mail([p.email], subject, body))
        res.sent += 1
    return _finish(notion, settings, event, backend, res, "retour", update_status)


CAMPAIGNS = {"invitation": invitation, "relance": relance, "recap-kam": recap_kam, "retour": retour}
