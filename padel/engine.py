"""Moteur d'automatisation (phase 1 : mode test).

À chaque passage il PRÉPARE des communications (aperçus consultables dans le dashboard) et signale ce qui nécessite
une décision. Il n'envoie rien : aucun connecteur d'envoi n'est branché, et un envoi ne pourra venir que d'une
validation explicite. Chaque action est rejouable sans effet de bord : une clé d'idempotence empêche qu'une même
communication soit préparée deux fois pour un même événement et un même destinataire.

    python -m padel.engine            # passage réel (écrit les aperçus et le journal)
    python -m padel.engine --dry-run  # montre ce qui serait préparé, n'écrit rien
"""
from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import store
from .campaigns import _base
from .config import Settings, load_settings
from .db import init_db, make_engine
from .mailer import render

log = logging.getLogger("padel.engine")


@dataclass
class _Ev:
    """Adaptateur minimal : les gabarits de mail lisent name/date/venue."""
    name: str
    date: date | None
    venue: str


@dataclass
class EngineResult:
    today: date
    dry_run: bool
    invitations: int = 0
    relances: int = 0
    pendings: int = 0
    skipped_existing: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        mode = "SIMULATION" if self.dry_run else "RÉEL"
        return (f"[{mode}] {self.invitations} invitation(s) et {self.relances} relance(s) préparées, "
                f"{self.pendings} élément(s) à valider, {self.skipped_existing} déjà préparé(s) (doublon évité)")


def _templates(settings: Settings):
    return settings.templates()


def _rule(settings: Settings, key: str, default):
    return settings.get("engine", key, default=default)


def _offset_for(settings: Settings, task_key: str, fallback: int) -> int:
    for t in _templates(settings):
        if t.key == task_key:
            return t.offset
    return fallback


def run(engine, settings: Settings, today: date | None = None, dry_run: bool = False) -> EngineResult:
    today = today or date.today()
    res = EngineResult(today, dry_run)
    inv_offset = _offset_for(settings, "invitation", 27)
    rel_offset = _offset_for(settings, "relance", 20)
    relance_after = timedelta(days=int(_rule(settings, "relance_after_days", 7)))
    with engine.connect() as cx:
        tx = cx.begin()
        try:
            for ev in store.list_events(cx):
                if not ev["date"] or ev["date"] < today or ev["status"] == "termine":
                    continue
                ctx_ev = _Ev(ev["name"], ev["date"], ev["venue"] or "")
                base = _base(settings, ctx_ev)
                invite_due = ev["date"] - timedelta(days=inv_offset) <= today
                relance_due = ev["date"] - timedelta(days=rel_offset) <= today
                regs = store.list_registrations(cx, ev["id"])
                for r in regs:
                    who = f"{r['prenom']} {r['nom']}".strip() or r["email"] or f"inscription {r['id']}"
                    if (r["type"] or "").lower() == "cocktail":
                        continue
                    if r["invite_status"] == "a_inviter" and invite_due:
                        if not r["email"]:
                            res.pendings += store.add_pending(
                                cx, f"noemail:{ev['id']}:{r['person_id']}", "donnee_manquante",
                                f"E-mail manquant : {who} ({ev['name']})",
                                "L'invitation est due mais la personne n'a pas d'adresse e-mail.", ev["id"])
                            continue
                        subject, body = render("invitation", p=r, **base)
                        new = store.record_communication(cx, f"invitation:{ev['id']}:{r['person_id']}", "invitation",
                                                         ev["id"], r["person_id"], r["email"], subject, body)
                        res.invitations += new
                        res.skipped_existing += not new
                    elif r["invite_status"] == "invite" and relance_due and r["email"]:
                        invited = r["invited_at"]
                        if invited and invited + relance_after > datetime.combine(today, datetime.min.time()):
                            continue  # trop tôt : on laisse le temps de répondre
                        subject, body = render("relance", p=r, **base)
                        new = store.record_communication(cx, f"relance:{ev['id']}:{r['person_id']}:1", "relance",
                                                         ev["id"], r["person_id"], r["email"], subject, body)
                        res.relances += new
                        res.skipped_existing += not new
            if not dry_run:
                store.log(cx, "engine_run", None, res.summary())
            if dry_run:
                tx.rollback()
            else:
                tx.commit()
        except Exception:
            tx.rollback()
            raise
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m padel.engine", description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="n'écrit rien dans la base")
    ap.add_argument("--today", help="forcer la date du jour (AAAA-MM-JJ)")
    ap.add_argument("--config")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = load_settings(args.config)
    engine = make_engine()
    init_db(engine)
    result = run(engine, settings, date.fromisoformat(args.today) if args.today else None, args.dry_run)
    print(result.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
