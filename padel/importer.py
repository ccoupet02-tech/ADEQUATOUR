"""Import des exports CSV de Notion (événements, tâches, participants) dans la base de l'application.

Les en-têtes sont reconnus sans tenir compte des accents ni de la casse. Les relations Notion sont exportées sous
la forme « DIJON (https://www.notion.so/...) » : seul le nom est conservé. Chaque import est rejouable : il complète
les champs vides sans écraser ce qui a déjà été saisi dans l'application.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import and_, delete, select, update

from . import store
from .db import people, registrations
from .config import Settings
from .util import base_task_name, norm

MONTHS = {"janvier": 1, "janv": 1, "fevrier": 2, "fevr": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
          "juillet": 7, "juil": 7, "aout": 8, "septembre": 9, "sept": 9, "octobre": 10, "novembre": 11,
          "decembre": 12, "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
          "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
          "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11,
          "dec": 12}
TASK_MAP = {"not started": "todo", "a faire": "todo", "todo": "todo", "to do": "todo", "in progress": "doing",
            "en cours": "doing", "done": "done", "termine": "done", "terminee": "done", "fait": "done"}
INVITE_MAP = {"not started": "a_inviter", "a inviter": "a_inviter", "pas de reponse": "invite", "invite": "invite",
              "acceptee": "accepte", "accepte": "accepte", "refusee": "refuse", "refuse": "refuse",
              "erreur mail": "erreur", "erreur": "erreur"}
EVENT_STATUS = {"not started": "preparation", "in progress": "en_cours", "done": "termine"}
_SPLIT_RELATION = re.compile(r"\s*\(https?://[^)]*\)\s*")


@dataclass
class Report:
    kind: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: list[str] = field(default_factory=list)

    def line(self) -> str:
        w = f" · {len(self.warnings)} avertissement(s)" if self.warnings else ""
        return f"{self.kind} : {self.created} créé(s), {self.updated} complété(s), {self.skipped} ignoré(s){w}"


def parse_any_date(value) -> date | None:
    """ISO, 14/10/2026, « 11 juin 2026 », « June 11, 2026 » ; une plage « a → b » garde la 1re date."""
    if value is None or not str(value).strip():
        return None
    text = str(value).split("→")[0].strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$", text)
    if m:
        return date(int(m[3]), int(m[2]), int(m[1]))
    t = norm(text.replace(",", " "))
    m = re.match(r"^(\d{1,2}) ([a-z]+) (\d{4})$", t)
    if m and m[2] in MONTHS:
        return date(int(m[3]), MONTHS[m[2]], int(m[1]))
    m = re.match(r"^([a-z]+) (\d{1,2}) (\d{4})$", t)
    if m and m[1] in MONTHS:
        return date(int(m[3]), MONTHS[m[1]], int(m[2]))
    raise ValueError(f"date non reconnue : {value!r}")


def relation_name(value) -> str:
    """'DIJON (https://notion.so/...), RENNES (https://...)' -> 'DIJON' (1re relation)."""
    text = _SPLIT_RELATION.sub("|", str(value or "")).strip("| ")
    return text.split("|")[0].strip()


def read_rows(source) -> list[dict]:
    """CSV (chemin, texte ou octets) -> lignes dont les clés sont normalisées (sans accents, minuscules)."""
    if isinstance(source, bytes):
        text = source.decode("utf-8-sig", errors="replace")
    elif hasattr(source, "read"):
        raw = source.read()
        text = raw.decode("utf-8-sig", errors="replace") if isinstance(raw, bytes) else raw
    elif isinstance(source, str) and "\n" in source:
        text = source
    else:
        with open(source, encoding="utf-8-sig", newline="") as fh:
            text = fh.read()
    sample = text[:2000]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    return [{norm(k): (v or "").strip() for k, v in row.items() if k} for row in reader]


def pick(row: dict, *aliases: str) -> str:
    for a in aliases:
        v = row.get(norm(a))
        if v:
            return v
    return ""


def _clean_phone(value: str) -> str:
    return value.lstrip("'").strip()


# ----------------------------------------------------------------------------- événements
def import_events(cx, source) -> Report:
    rep = Report("Événements")
    for i, row in enumerate(read_rows(source), start=2):
        name = pick(row, "Name", "Nom", "Ville", "Événement")
        if not name:
            rep.skipped += 1
            continue
        try:
            when = parse_any_date(pick(row, "Date"))
        except ValueError as exc:
            rep.warnings.append(f"ligne {i} ({name}) : {exc}")
            when = None
        dr = pick(row, "DR", "Responsable")
        notes = []
        for label, col in (("participants", "Participants"), ("tournoi", "Tournoi"), ("initiation", "Initiation"),
                           ("staff", "Staff")):
            if pick(row, col).isdigit():
                notes.append(f"{label} {pick(row, col)}")
        eid, created = store.upsert_event(
            cx, name, date=when, venue=pick(row, "Lieu", "Salle"), dr="" if dr.upper() == "TBD" else dr,
            status=EVENT_STATUS.get(norm(pick(row, "Status", "Statut")), None) or None,
            capacity=int(pick(row, "Participants")) if pick(row, "Participants").isdigit() else None,
            courts=int(pick(row, "Terrain")) if pick(row, "Terrain").isdigit() else None,
            notes=("Prévu (Notion) : " + ", ".join(notes)) if notes else "")
        rep.created += created
        rep.updated += not created
    store.log(cx, "import_events", None, rep.line(), "import")
    return rep


# ----------------------------------------------------------------------------- tâches
def import_tasks(cx, source, templates=None) -> Report:
    """Importe la to-do Notion. Les doublons « Invitation » / « Invitation (1) » d'un même événement sont fusionnés
    en gardant la tâche la plus avancée ; une tâche sans événement ou sans nom est ignorée."""
    rep = Report("Tâches")
    best: dict[tuple[str, str], dict] = {}
    order = {"todo": 0, "doing": 1, "done": 2}
    for i, row in enumerate(read_rows(source), start=2):
        name = pick(row, "Tache", "Tâche", "Name", "Nom")
        ev = relation_name(pick(row, "Event", "Événement", "Tournoi"))
        if not name or not ev:
            rep.skipped += 1
            continue
        status = TASK_MAP.get(norm(pick(row, "Statut", "Status")), "todo")
        offset = pick(row, "Offset")
        item = {"name": base_task_name(name), "category": pick(row, "Catégorie", "Categorie"),
                "offset": int(float(offset)) if re.match(r"^-?\d+(\.0+)?$", offset) else None, "status": status}
        key = (norm(ev), store.task_key(name))
        prev = best.get(key)
        if prev is None or order[status] > order[prev[1]["status"]]:
            best[key] = (ev, item)
        else:
            rep.skipped += 1
    from sqlalchemy import select, update
    from .db import tasks
    for ev, item in best.values():
        event = store.get_event(cx, ev)
        if not event:
            rep.warnings.append(f"événement inconnu : {ev} (importez d'abord les événements)")
            rep.skipped += 1
            continue
        key = store.task_key(item["name"])
        row = cx.execute(select(tasks).where((tasks.c.event_id == event["id"]) & (tasks.c.key == key))).mappings().first()
        if row:
            vals = {}
            if order[item["status"]] > order[row["status"]]:
                vals["status"] = item["status"]
                if item["status"] == "done":
                    vals["done_at"] = store.now()
            if row["offset_days"] is None and item["offset"] is not None:
                vals["offset_days"] = item["offset"]
            if vals:
                cx.execute(update(tasks).where(tasks.c.id == row["id"]).values(**vals))
                rep.updated += 1
        else:
            from sqlalchemy import insert
            cx.execute(insert(tasks).values(event_id=event["id"], key=key, name=item["name"],
                                            category=item["category"], offset_days=item["offset"],
                                            status=item["status"],
                                            done_at=store.now() if item["status"] == "done" else None))
            rep.created += 1
    store.log(cx, "import_tasks", None, rep.line(), "import")
    return rep


# ----------------------------------------------------------------------------- participants
def import_participants(cx, source, default_event: str | None = None, settings: Settings | None = None) -> Report:
    from .participants import normalize_size, normalize_type
    rep = Report("Participants")
    sizes = settings.get("sizes") if settings else None
    for i, row in enumerate(read_rows(source), start=2):
        prenom, nom = pick(row, "Prenom", "Prénom"), pick(row, "Nom")
        email = pick(row, "Email", "Mail", "E-mail")
        if not (prenom or nom or email):
            rep.skipped += 1
            continue
        ev = relation_name(pick(row, "Tournoi", "Event", "Événement")) or (default_event or "")
        event = store.get_event(cx, ev) if ev else None
        if not event:
            rep.warnings.append(f"ligne {i} ({prenom} {nom}) : événement « {ev or '?'} » introuvable")
            rep.skipped += 1
            continue
        pid, p_new = store.upsert_person(
            cx, prenom=prenom, nom=nom, email=email, phone=_clean_phone(pick(row, "Phone", "Téléphone", "Telephone")),
            company=pick(row, "Entreprise", "Société"), position=pick(row, "Position", "Fonction"),
            sector=pick(row, "Sector", "Secteur"), source="import notion")
        raw_type = pick(row, "Type")
        typ = normalize_type(raw_type, settings) or raw_type
        size_raw = pick(row, "Size", "Taille")
        size = normalize_size(size_raw, sizes) or ""
        if size_raw and not size:
            rep.warnings.append(f"ligne {i} ({prenom} {nom}) : taille « {size_raw} » non reconnue")
        _, r_new = store.upsert_registration(
            cx, event["id"], pid, type=typ, team=pick(row, "Team", "Équipe"), poule=pick(row, "Poule"),
            size=size, level=pick(row, "Level", "Niveau"), source="import notion",
            invite_status=INVITE_MAP.get(norm(pick(row, "Statut invitation", "Invitation")), "a_inviter"))
        rep.created += r_new
        rep.updated += not r_new
    store.log(cx, "import_participants", None, rep.line(), "import")
    return rep


# ----------------------------------------------------------------------------- inscriptions (formulaire)
def import_signups(cx, source, event: str, settings: Settings | None = None, dry_run: bool = False,
                   mirror: bool = False) -> Report:
    """Importe l'export du formulaire (Google Sheet / Typeform). Une ligne = 1 inscrit ou 2 joueurs en équipe.
    Rejouable sans doublon : une personne déjà connue est complétée, jamais dupliquée.
    mirror=True (synchro Typeform) : la base reflète exactement le formulaire — la dernière inscription d'une personne
    remplace les anciennes, et les inscriptions « formulaire » qui n'y figurent plus (réponse supprimée) sont retirées."""
    from .signups import parse_signups
    rep = Report("Inscriptions")
    ev = store.get_event(cx, event)
    if not ev:
        rep.warnings.append(f"événement « {event} » introuvable")
        return rep
    signups, warns, _ = parse_signups(source if isinstance(source, list) else read_rows(source), settings)
    rep.warnings.extend(warns)
    teams = solos = 0
    keep_ids: set[int] = set()
    keep_keys: set[str] = set()
    for s in signups:
        for p in s["players"]:
            keep_keys.add(store.person_key(p["prenom"], p["nom"], p["email"], p["company"]))
            if dry_run:
                known = cx.execute(select(people.c.id).where(people.c.pkey == store.person_key(
                    p["prenom"], p["nom"], p["email"], p["company"]))).first()
                rep.created += 0 if known else 1
                rep.updated += 1 if known else 0
                continue
            pid, _ = store.upsert_person(cx, prenom=p["prenom"], nom=p["nom"], email=p["email"], phone=_clean_phone(p["phone"]),
                                         company=p["company"], position=p.get("position", ""), source="formulaire")
            keep_ids.add(pid)
            rid, new = store.upsert_registration(
                cx, ev["id"], pid, type=p["type"] or "Tournoi", team=s["team"], size=p["size"], level=p["level"],
                stakes=s["stakes"], registered_at=s["registered_at"], source="formulaire", invite_status="accepte")
            if mirror and not new:   # la dernière inscription fait foi, y compris pour l'équipe (même vide)
                cx.execute(update(registrations).where(registrations.c.id == rid).values(
                    team=s["team"], level=p["level"] or None, stakes=s["stakes"], type=p["type"] or "Tournoi",
                    registered_at=s["registered_at"] or registrations.c.registered_at))
            rep.created += new
            rep.updated += not new
        teams += len(s["players"]) == 2
        solos += len(s["players"]) == 1
    if mirror and signups:
        stale = [r for r in cx.execute(select(registrations.c.id, registrations.c.person_id, people.c.pkey).join(
            people, people.c.id == registrations.c.person_id).where(and_(
                registrations.c.event_id == ev["id"], registrations.c.source == "formulaire"))).mappings()
            if r["pkey"] not in keep_keys]
        if stale and not dry_run:
            cx.execute(delete(registrations).where(registrations.c.id.in_([r["id"] for r in stale])))
            cx.execute(delete(people).where(and_(people.c.source == "formulaire", people.c.id.in_([r["person_id"] for r in stale]),
                                                 people.c.id.not_in(select(registrations.c.person_id)))))
        if stale:
            rep.warnings.append(f"{len(stale)} ancienne(s) inscription(s) du formulaire " + ("seraient retirées" if dry_run else "retirées")
                                + " (remplacées par une inscription plus récente ou réponse supprimée dans Typeform).")
    rep.warnings.insert(0, f"{teams} équipe(s) de 2 · {solos} inscrit(s) seul(s)")
    if not dry_run:
        store.log(cx, "import_signups", ev["id"], rep.line() + f" ({teams} équipes, {solos} solos)", "import")
    return rep


def sync_typeform(cx, event: str, client, settings: Settings | None = None, dry_run: bool = False) -> Report:
    """Lit les réponses du formulaire Typeform de l'événement et les importe (même logique que le CSV)."""
    from .signups import typeform_rows
    ev = store.get_event(cx, event)
    rep = Report("Typeform")
    if not ev or not (ev.get("typeform_id") or "").strip():
        rep.warnings.append("Aucun formulaire Typeform associé à cet événement.")
        return rep
    form_id = ev["typeform_id"].strip()
    rows = typeform_rows(client.get_form(form_id), list(client.iter_responses(form_id)))
    if not rows:
        rep.warnings.append("Aucune réponse complète sur ce formulaire pour l'instant.")
        return rep
    return import_signups(cx, rows, event, settings, dry_run=dry_run, mirror=True)


# ----------------------------------------------------------------------------- staff
def import_staff(cx, source, event: str) -> Report:
    """Importe un tableau Staff (colonnes Name, Prenom, Role, Shirt size, Hotel, Transport, Repas) pour un événement."""
    rep = Report("Staff")
    ev = store.get_event(cx, event)
    if not ev:
        rep.warnings.append(f"événement « {event} » introuvable")
        return rep
    rows = store.list_staff(cx, ev["id"])
    for row in read_rows(source):
        item = {"nom": pick(row, "Name", "Nom"), "prenom": pick(row, "Prenom", "Prénom"), "role": pick(row, "Role", "Rôle"),
                "shirt_size": pick(row, "Shirt size", "Taille"), "hotel": pick(row, "Hotel", "Hôtel"),
                "transport": pick(row, "Transport"), "repas": pick(row, "Repas")}
        if not (item["nom"] or item["prenom"]):
            rep.skipped += 1
            continue
        if any(norm(r["nom"]) == norm(item["nom"]) and norm(r["prenom"]) == norm(item["prenom"]) for r in rows):
            rep.skipped += 1
            continue
        rows.append(item)
        rep.created += 1
    store.save_staff(cx, ev["id"], rows, "import")
    return rep


# ----------------------------------------------------------------------------- jeu de départ
# Événements relevés dans l'espace Notion le 14/10/2026 (nom, date, lieu, responsable, statut, prévu, terrains).
SEED_EVENTS = [
    ("PACA", "2026-06-11", "La Pista", "Chris FACCHIN", "Done", 70, 5),
    ("PERIGUEUX", "2026-07-09", "La Bandeja", "Virginie SAGOT", "Done", 50, 5),
    ("DIJON", "2026-10-01", "Le Norges", "Patrice GIRAUDO", "Not started", 50, 6),
    ("RENNES", "2026-10-08", "PadelShot", "Delphine TREMELO", "In progress", 60, 7),
    ("ANGERS", "2026-10-15", "La Pala", "Delphine TREMELO", "In progress", 60, 6),
    ("BORDEAUX", "2026-11-19", "3D Padel", "Didier GARCIA", "Not started", None, None),
    ("LYON", "2026-11-26", "Esprit", "Christelle FABRE", "Not started", None, None),
    ("ORLEANS", "2026-12-03", "Make it padel", "Rodolphe VILAIN", "Not started", None, None),
    ("LILLE", "2027-01-21", "", "", "Not started", None, None),
    ("SAINT ETIENNE", "2027-02-04", "", "Loic FERTILLE", "Not started", None, None),
    ("VALENCE", "2027-02-11", "", "", "Not started", None, None),
    ("TOULOUSE", "2027-02-25", "", "Didier GARCIA", "Not started", None, None),
    ("ILE DE FRANCE", "2027-03-11", "", "Benoit PIFFETEAU", "Not started", None, None),
    ("GRAND FINALE", "2027-03-18", "", "", "Not started", None, None),
]


def seed_events(cx, settings: Settings) -> Report:
    """Charge les 14 événements connus et leurs 22 tâches de référence (sans écraser l'existant)."""
    rep = Report("Jeu de départ")
    for name, d, venue, dr, status, cap, courts in SEED_EVENTS:
        eid, created = store.upsert_event(cx, name, date=date.fromisoformat(d), venue=venue, dr=dr,
                                          status=EVENT_STATUS[norm(status)], capacity=cap, courts=courts)
        store.ensure_tasks(cx, eid, settings.templates())
        rep.created += created
        rep.updated += not created
    store.log(cx, "seed", None, rep.line(), "import")
    return rep
