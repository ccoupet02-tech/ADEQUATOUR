"""Moteur d'automatisation (phase 1 : mode aperçu).

Chaque workflow a un déclencheur (échéance atteinte), des conditions (événement à venir, personne éligible, e-mail
présent, pas de doublon), une action (préparer un aperçu de communication), un statut (ok / rien / erreur /
simulation), un historique (table workflow_runs) et une stratégie de reprise : une erreur sur un événement
n'arrête pas les autres, le passage suivant réessaie (l'idempotence évite tout doublon) et, après 3 échecs
consécutifs, le cas remonte dans « À valider ».

Il N'ENVOIE RIEN : aucun connecteur d'envoi n'est branché. Un envoi ne pourra venir que d'une validation explicite.

    python -m padel.engine            # passage réel (écrit les aperçus et le journal)
    python -m padel.engine --dry-run  # montre ce qui serait préparé, n'écrit rien
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import traceback
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
    errors: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        mode = "SIMULATION" if self.dry_run else "RÉEL"
        err = f", {self.errors} erreur(s)" if self.errors else ""
        return (f"[{mode}] {self.invitations} invitation(s) et {self.relances} relance(s) préparées, "
                f"{self.pendings} élément(s) à valider, {self.skipped_existing} déjà préparé(s) (doublon évité){err}")


def _offset_for(settings: Settings, task_key: str, fallback: int) -> int:
    for t in settings.templates():
        if t.key == task_key:
            return t.offset
    return fallback


# ----------------------------------------------------------------------------- workflows
def wf_invitations(cx, settings: Settings, ev: dict, today: date, res: EngineResult) -> tuple[int, int]:
    """Déclencheur : J-offset de la tâche « Invitation ». Prépare l'aperçu pour chaque personne à inviter."""
    if ev["date"] - timedelta(days=_offset_for(settings, "invitation", 27)) > today:
        return 0, 0
    base = _base(settings, _Ev(ev["name"], ev["date"], ev["venue"] or ""))
    prepared = flagged = 0
    for r in store.list_registrations(cx, ev["id"]):
        if r["invite_status"] != "a_inviter" or (r["type"] or "").lower() == "cocktail":
            continue
        who = f"{r['prenom']} {r['nom']}".strip() or r["email"] or f"inscription {r['id']}"
        if not r["email"]:
            new = store.add_pending(cx, f"noemail:{ev['id']}:{r['person_id']}", "donnee_manquante",
                                    f"E-mail manquant : {who} ({ev['name']})",
                                    "L'invitation est due mais la personne n'a pas d'adresse e-mail.", ev["id"])
            flagged += new
            continue
        subject, body = render("invitation", p=r, **base)
        if store.record_communication(cx, f"invitation:{ev['id']}:{r['person_id']}", "invitation", ev["id"],
                                      r["person_id"], r["email"], subject, body):
            prepared += 1
        else:
            res.skipped_existing += 1
    res.invitations += prepared
    res.pendings += flagged
    return prepared, flagged


def wf_relances(cx, settings: Settings, ev: dict, today: date, res: EngineResult) -> tuple[int, int]:
    """Déclencheur : J-offset de la tâche « Relance » ET délai écoulé depuis l'invitation. Une réponse annule."""
    if ev["date"] - timedelta(days=_offset_for(settings, "relance", 20)) > today:
        return 0, 0
    wait = timedelta(days=int(settings.get("engine", "relance_after_days", default=7)))
    limit = datetime.combine(today, datetime.min.time())
    base = _base(settings, _Ev(ev["name"], ev["date"], ev["venue"] or ""))
    prepared = 0
    for r in store.list_registrations(cx, ev["id"]):
        if r["invite_status"] != "invite" or not r["email"]:
            continue
        if r["invited_at"] and r["invited_at"] + wait > limit:
            continue
        subject, body = render("relance", p=r, **base)
        if store.record_communication(cx, f"relance:{ev['id']}:{r['person_id']}:1", "relance", ev["id"],
                                      r["person_id"], r["email"], subject, body):
            prepared += 1
        else:
            res.skipped_existing += 1
    res.relances += prepared
    return prepared, 0


def wf_typeform(cx, settings: Settings, ev: dict, today: date, res: EngineResult) -> tuple[int, int]:
    """Récupère les nouvelles inscriptions du formulaire Typeform de l'événement (si un jeton est configuré)."""
    if not (ev.get("typeform_id") or "").strip() or not settings.env.get("TYPEFORM_TOKEN"):
        return 0, 0   # rien à faire : on ne simule jamais une synchronisation
    from .importer import sync_typeform
    rep = sync_typeform(cx, ev["name"], settings.typeform_client(), settings)
    flagged = 0
    for w in rep.warnings:
        if w.startswith("ligne") and ("non reconnu" in w or "incomplet" in w):
            flagged += store.add_pending(cx, f"tf:{ev['id']}:{hashlib.md5(w.encode()).hexdigest()[:10]}", "donnee_manquante",
                                         f"Inscription à vérifier ({ev['name']})", w, ev["id"])
    return rep.created, flagged


WORKFLOWS = {"typeform": wf_typeform, "invitations": wf_invitations, "relances": wf_relances}


# ----------------------------------------------------------------------------- exécution
def run(engine, settings: Settings, today: date | None = None, dry_run: bool = False, trigger: str = "planifie",
        only_event: int | None = None) -> EngineResult:
    today = today or date.today()
    res = EngineResult(today, dry_run)
    with engine.connect() as cx:
        outer = cx.begin()
        try:
            store.ensure_automations(cx)
            for ev in store.list_events(cx):
                if only_event is not None and ev["id"] != only_event:
                    continue
                if not ev["date"] or ev["date"] < today or ev["status"] in ("termine", "annule"):
                    continue
                for key, fn in WORKFLOWS.items():
                    if not store.is_enabled(cx, key):
                        continue
                    started = store.now()
                    sp = cx.begin_nested()
                    try:
                        prepared, flagged = fn(cx, settings, ev, today, res)
                        sp.commit()
                        status = "simulation" if dry_run else ("ok" if (prepared or flagged) else "rien")
                        if status != "rien" or dry_run:
                            store.record_run(cx, key, ev["id"], status, prepared, flagged, trigger=trigger,
                                             started_at=started)
                    except Exception as exc:  # noqa: BLE001  (isolation : un événement ne bloque pas les autres)
                        sp.rollback()
                        res.errors += 1
                        res.notes.append(f"{ev['name']} / {key} : {exc}")
                        log.error("workflow %s / %s en erreur : %s", key, ev["name"], exc)
                        store.record_run(cx, key, ev["id"], "erreur", error=f"{exc}\n{traceback.format_exc()[-1200:]}",
                                         trigger=trigger, started_at=started)
            if not dry_run:
                store.log(cx, "engine_run", None, res.summary())
            if dry_run:
                outer.rollback()
            else:
                outer.commit()
        except Exception:
            outer.rollback()
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
    for n in result.notes:
        print("  !", n)
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
