"""Ligne de commande : python -m padel <commande> [options]

Garde-fous :
  * sans --apply  : AUCUNE écriture dans Notion (simulation, tout est affiché) ;
  * sans --send   : AUCUN mail ni notification envoyé (les mails sont écrits en .eml pour relecture) ;
  * --send exige --apply (sinon les statuts ne seraient pas mis à jour et les envois seraient refaits).
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from . import __version__
from .audit import audit_tasks
from .campaigns import CAMPAIGNS
from .config import Settings, load_settings
from .documents import diplomas_pdf, labels_pdf, planning_pdf, poules_pdf, referee_pdf
from .events import Event, create_event, find_event, list_events
from .logistics import logistics_report, read_staff, write_logistics
from .mailer import get_backend
from .notify import build_digest, deliver, relevant_events
from .participants import (active_participants, fetch_participants, read_people_csv, sync_participants,
                           upsert_people)
from .summary import refresh_summary
from .tasks import ensure_event_tasks, fetch_tasks, mark_task
from .tournament import plan_tournament, write_poules_to_notion
from .typeform import flatten_fields, match_fields
from .util import fr_date, norm, slug

log = logging.getLogger("padel")


class Ctx:
    def __init__(self, args):
        self.settings: Settings = load_settings(args.config)
        self.apply: bool = args.apply
        self.send: bool = args.send
        self.today: date = date.fromisoformat(args.today) if args.today else date.today()
        if self.send and not self.apply:
            raise ValueError("--send exige --apply (sinon statuts et tâches ne seraient pas mis à jour).")
        self.notion = self.settings.notion_client(dry_run=not self.apply)

    def mode(self) -> str:
        return ("ÉCRITURE Notion" if self.apply else "SIMULATION (rien n'est écrit dans Notion ; ajoutez --apply)")

    def event(self, ident: str) -> Event:
        return find_event(list_events(self.notion, self.settings), ident)


# ----------------------------------------------------------------------------- commandes
def cmd_list_events(ctx: Ctx, args) -> int:
    for e in list_events(ctx.notion, ctx.settings):
        when = "" if not e.date else f"  (J{(e.date - ctx.today).days:+d})"
        print(f"{fr_date(e.date):>12}  {e.name:<14} {e.venue}{when}")
    return 0


def cmd_audit(ctx: Ctx, args) -> int:
    events = list_events(ctx.notion, ctx.settings)
    issues = audit_tasks(ctx.settings, fetch_tasks(ctx.notion, ctx.settings), events)
    print(f"{len(issues)} point(s) à traiter\n" if issues else "Rien à signaler.")
    for i in issues:
        print(" -", i)
    return 0


def cmd_create_event(ctx: Ctx, args) -> int:
    print(f"[{ctx.mode()}]")
    event, created = create_event(
        ctx.notion, ctx.settings, args.name, date.fromisoformat(args.date), venue=args.venue, dr=args.dr,
        price=args.price, contact_name=args.contact_name, contact_phone=args.contact_phone,
        contact_email=args.contact_email, courts_indoor=args.indoor, courts_outdoor=args.outdoor,
        workshop_room=args.workshop_room, with_tasks=not args.no_tasks)
    print(f"Événement « {event.name} » ({fr_date(event.date)}) : page + base Staff + {len(created)} tâche(s)")
    return 0


def cmd_sync_tasks(ctx: Ctx, args) -> int:
    print(f"[{ctx.mode()}]")
    events = list_events(ctx.notion, ctx.settings)
    if args.all:  # on ignore les événements déjà passés : leurs tâches seraient toutes « en retard »
        grace = ctx.settings.get("digest", "grace_days", default=14)
        targets = [e for e in events if e.date is None or (ctx.today - e.date).days <= grace]
        for e in events:
            if e not in targets:
                print(f"{e.name:<14} ignoré (événement passé le {fr_date(e.date)})")
    else:
        targets = [find_event(events, args.event)]
    for e in targets:
        created = ensure_event_tasks(ctx.notion, ctx.settings, e)
        print(f"{e.name:<14} {len(created)} tâche(s) créée(s)" + (f" : {', '.join(created[:3])}…" if len(created) > 3 else
                                                                  (f" : {', '.join(created)}" if created else "")))
    return 0


def cmd_typeform_fields(ctx: Ctx, args) -> int:
    e = ctx.event(args.event)
    form_id = ctx.settings.typeform_form(e.name)
    if not form_id:
        raise LookupError(f"Aucun formulaire configuré pour {e.name} (typeform.forms dans config.yaml)")
    fields = flatten_fields(ctx.settings.typeform_client().get_form(form_id).get("fields"))
    mapping = match_fields(fields, ctx.settings.get("typeform", "fields"), ctx.settings.get("typeform", "refs"))
    titles = {f["id"]: f["title"] for f in fields}
    print(f"{len(fields)} question(s) dans le formulaire {form_id}\n")
    for key, ids in mapping.items():
        print(f"{key:<14} -> " + (" | ".join(titles[i] for i in ids) if ids else "(non reconnu)"))
    used = {i for ids in mapping.values() for i in ids}
    ignored = [t for i, t in titles.items() if i not in used]
    if ignored:
        print("\nQuestions ignorées :", " | ".join(ignored))
    return 0


def cmd_sync_participants(ctx: Ctx, args) -> int:
    print(f"[{ctx.mode()}]")
    e = ctx.event(args.event)
    print(sync_participants(ctx.notion, ctx.settings, e, ctx.settings.typeform_client(), args.overwrite))
    return 0


def cmd_import_csv(ctx: Ctx, args) -> int:
    print(f"[{ctx.mode()}]")
    e = ctx.event(args.event)
    people = read_people_csv(args.file, ctx.settings)
    print(f"{len(people)} ligne(s) lues -> {upsert_people(ctx.notion, ctx.settings, e, people, args.overwrite)}")
    return 0


def _event_out(ctx: Ctx, e: Event) -> Path:
    return ctx.settings.out_dir / slug(e.name)


def cmd_documents(ctx: Ctx, args) -> int:
    print(f"[{ctx.mode()}]")
    e = ctx.event(args.event)
    if not e.date:
        raise ValueError(f"{e.name} n'a pas de date : renseignez-la dans Notion.")
    participants = fetch_participants(ctx.notion, ctx.settings, e.id)
    plan = plan_tournament(participants, ctx.settings, e.date, args.poule_size, args.courts, args.minutes, args.start)
    for poule in plan.poules:  # étiquettes cohérentes avec les poules calculées
        for team in poule.teams:
            for m in team.members:
                m.poule = f"Poule {poule.name}"
    out, brand = _event_out(ctx, e), ctx.settings.brand
    files = [planning_pdf(out / "planning.pdf", brand, e.name, e.date, plan),
             poules_pdf(out / "poules.pdf", brand, e.name, plan),
             referee_pdf(out / "arbitres.pdf", brand, e.name, plan),
             diplomas_pdf(out / "diplomes.pdf", brand, e.name, e.date, active_participants(participants, ctx.settings)),
             labels_pdf(out / "etiquettes.pdf", brand, e.name, active_participants(participants, ctx.settings))]
    print(f"{len(plan.teams)} équipes · {len(plan.poules)} poule(s) · {len(plan.matches)} matchs · "
          f"{plan.start:%H:%M} -> {plan.end:%H:%M}")
    for w in plan.warnings:
        print("  !", w)
    for f in files:
        print("  ->", f)
    if args.write_poules:
        print(f"{write_poules_to_notion(ctx.notion, ctx.settings, plan)} fiche(s) mises à jour (Poule, Position)")
    if ctx.apply:
        tasks = fetch_tasks(ctx.notion, ctx.settings, e.id)
        for key in ("poules", "documents", "etiquettes"):
            mark_task(ctx.notion, ctx.settings, e, key, ctx.settings.status("in_progress"), tasks)
    return 0


def cmd_logistics(ctx: Ctx, args) -> int:
    e = ctx.event(args.event)
    report = logistics_report(ctx.settings, e, fetch_participants(ctx.notion, ctx.settings, e.id),
                              read_staff(ctx.notion, ctx.settings, e))
    paths = write_logistics(_event_out(ctx, e), report)
    print(paths[0].read_text(encoding="utf-8"))
    for p in paths:
        print("  ->", p)
    return 0


def cmd_mail(ctx: Ctx, args) -> int:
    print(f"[{ctx.mode()}] [{'ENVOI RÉEL' if ctx.send else 'aucun envoi : .eml'}]")
    e = ctx.event(args.event)
    backend = get_backend(ctx.settings, ctx.send, _event_out(ctx, e) / "mails")
    participants = fetch_participants(ctx.notion, ctx.settings, e.id)
    print(CAMPAIGNS[args.kind](ctx.notion, ctx.settings, e, participants, backend))
    if not backend.real:
        print(f"Relire les .eml dans {_event_out(ctx, e) / 'mails'} puis relancer avec --apply --send.")
    return 0


def _digest(ctx: Ctx, items) -> str:
    text = build_digest(ctx.settings, ctx.today, items)
    backend = get_backend(ctx.settings, ctx.send, ctx.settings.out_dir / "digest")
    used = deliver(ctx.settings, text, backend, webhook=ctx.send)
    print(text)
    print(f"\n[envoyé via : {', '.join(used) if used else 'aucun canal configuré ou --send absent'}]")
    return text


def _relevant(ctx: Ctx) -> list[Event]:
    cfg = ctx.settings.get("digest", default={})
    return relevant_events(list_events(ctx.notion, ctx.settings), ctx.today,
                           cfg.get("horizon_days", 45), cfg.get("grace_days", 14))


def cmd_digest(ctx: Ctx, args) -> int:
    _digest(ctx, [(e, fetch_tasks(ctx.notion, ctx.settings, e.id)) for e in _relevant(ctx)])
    return 0


def cmd_refresh(ctx: Ctx, args) -> int:
    print(f"[{ctx.mode()}]")
    events = [ctx.event(args.event)] if args.event else _relevant(ctx)
    for e in events:
        refresh_summary(ctx.notion, ctx.settings, e, fetch_tasks(ctx.notion, ctx.settings, e.id),
                        fetch_participants(ctx.notion, ctx.settings, e.id), ctx.today)
        print(f"Synthèse mise à jour : {e.name}")
    return 0


def cmd_daily(ctx: Ctx, args) -> int:
    """Routine quotidienne : tâches manquantes, participants Typeform, synthèses, digest."""
    print(f"[{ctx.mode()}] {fr_date(ctx.today)}")
    items = []
    can_typeform = bool(ctx.settings.env.get("TYPEFORM_TOKEN"))
    for e in _relevant(ctx):
        created = ensure_event_tasks(ctx.notion, ctx.settings, e)
        line = f"{e.name:<14} +{len(created)} tâche(s)"
        if can_typeform and ctx.settings.typeform_form(e.name):
            try:
                line += f" · participants : {sync_participants(ctx.notion, ctx.settings, e, ctx.settings.typeform_client()).created} nouveau(x)"
            except Exception as exc:  # une panne Typeform ne doit pas bloquer le reste
                line += f" · Typeform en échec ({exc})"
                log.warning("Typeform %s : %s", e.name, exc)
        tasks = fetch_tasks(ctx.notion, ctx.settings, e.id)
        refresh_summary(ctx.notion, ctx.settings, e, tasks, fetch_participants(ctx.notion, ctx.settings, e.id), ctx.today)
        print(line)
        items.append((e, tasks))
    print()
    _digest(ctx, items)
    return 0


# ----------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="chemin d'un config.yaml alternatif")
    common.add_argument("--apply", action="store_true", help="écrire réellement dans Notion (sinon simulation)")
    common.add_argument("--send", action="store_true", help="envoyer réellement mails/notifications (exige --apply)")
    common.add_argument("--today", help="forcer la date du jour (AAAA-MM-JJ), pour tester")
    common.add_argument("-v", "--verbose", action="store_true")
    p = argparse.ArgumentParser(prog="python -m padel", description="Pipeline Adéquat Padel Tour",
                                epilog=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, parents=[common], help=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("list-events", cmd_list_events, "liste les événements")
    add("audit", cmd_audit, "repère les incohérences de la to-do")
    sp = add("create-event", cmd_create_event, "crée un événement (page, staff, tâches)")
    sp.add_argument("name"); sp.add_argument("date", help="AAAA-MM-JJ")
    for opt in ("venue", "dr", "contact-name", "contact-phone", "contact-email", "workshop-room"):
        sp.add_argument(f"--{opt}")
    sp.add_argument("--price", type=float)
    sp.add_argument("--indoor", type=int); sp.add_argument("--outdoor", type=int)
    sp.add_argument("--no-tasks", action="store_true")
    sp = add("sync-tasks", cmd_sync_tasks, "crée les tâches manquantes du référentiel")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--event"); g.add_argument("--all", action="store_true")
    sp = add("typeform-fields", cmd_typeform_fields, "montre quelles questions Typeform sont reconnues")
    sp.add_argument("--event", required=True)
    sp = add("sync-participants", cmd_sync_participants, "importe les réponses Typeform")
    sp.add_argument("--event", required=True); sp.add_argument("--overwrite", action="store_true")
    sp = add("import-csv", cmd_import_csv, "importe un CSV de participants")
    sp.add_argument("--event", required=True); sp.add_argument("file"); sp.add_argument("--overwrite", action="store_true")
    sp = add("documents", cmd_documents, "poules + planning + PDF (planning, poules, arbitres, diplômes, étiquettes)")
    sp.add_argument("--event", required=True)
    sp.add_argument("--poule-size", type=int); sp.add_argument("--courts", type=int)
    sp.add_argument("--minutes", type=int); sp.add_argument("--start", help="HH:MM")
    sp.add_argument("--write-poules", action="store_true", help="écrit Poule/Position dans Notion (avec --apply)")
    sp = add("logistics", cmd_logistics, "t-shirts, repas, hôtel/transport")
    sp.add_argument("--event", required=True)
    sp = add("mail", cmd_mail, "invitation | relance | recap-kam | retour")
    sp.add_argument("kind", choices=sorted(CAMPAIGNS)); sp.add_argument("--event", required=True)
    add("digest", cmd_digest, "tâches en retard / imminentes (webhook ou mail avec --send)")
    sp = add("refresh", cmd_refresh, "met à jour la synthèse de la page événement")
    sp.add_argument("--event")
    add("daily", cmd_daily, "routine quotidienne complète")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(Ctx(args), args)
    except (LookupError, ValueError, KeyError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        if args.verbose:
            raise
        print(f"Erreur inattendue : {exc} (relancer avec -v pour le détail)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
