"""Participants : lecture Notion, normalisation des réponses, import CSV, synchronisation Typeform."""
from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .typeform import flatten_fields, match_fields, parse_response
from .util import norm, norm_id

log = logging.getLogger("padel.participants")

MANAGED = ("prenom", "nom", "email", "telephone", "company", "team", "level", "size", "type")
DEFAULT_SIZES = ["S", "M", "L", "XL", "XXL"]


@dataclass
class Participant:
    id: str = ""
    prenom: str = ""
    nom: str = ""
    email: str = ""
    telephone: str = ""
    company: str = ""
    team: str = ""
    level: str = ""
    size: str = ""
    type: str = ""
    invite_status: str = ""
    poule: str = ""
    position: str = ""
    event_ids: list[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.prenom} {self.nom}".strip()


@dataclass
class SyncReport:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        s = f"{self.created} créés · {self.updated} mis à jour · {self.unchanged} inchangés"
        return s + "".join(f"\n  ! {w}" for w in self.warnings)


# --------------------------------------------------------------------------- lecture Notion
def fetch_participants(notion, settings: Settings, event_id: str | None = None) -> list[Participant]:
    s = settings.schema("participants")
    flt = s.filter("event", "contains", event_id) if event_id else None
    out = []
    for page in notion.query_database(settings.participants_db, flt):
        r = lambda k: s.read(page, k)  # noqa: E731
        out.append(Participant(
            id=page["id"], prenom=r("prenom") or "", nom=r("nom") or "", email=(r("email") or "").strip(),
            telephone=r("telephone") or "", company=r("company") or "", team=r("team") or "",
            level=r("level") or "", size=r("size") or "", type=r("type") or "",
            invite_status=r("invite_status") or "", poule=r("poule") or "",
            position=r("position") or "", event_ids=r("event") or []))
    return out


def is_invited(p: Participant, settings: Settings) -> bool:
    """Vrai si une invitation a déjà été envoyée (statut autre que vide / « Not started »)."""
    not_invited = norm(settings.get("invite_statuses", "not_invited", default="Not started"))
    return norm(p.invite_status) not in ("", not_invited)


def active_participants(participants: list[Participant], settings: Settings) -> list[Participant]:
    excluded = {norm(x) for x in settings.get("tournament", "exclude_invite_status", default=[])}
    return [p for p in participants if norm(p.invite_status) not in excluded]


# --------------------------------------------------------------------------- normalisation
def parse_level(text: str | None, settings: Settings | dict | None = None) -> float | None:
    """'3 - Intermédiaire' -> 3.0 ; 'Confirmé' -> 3.0 ; inconnu -> None."""
    if not text or not str(text).strip():
        return None
    t = norm(text)
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?)\b", t)
    if m:
        return float(m.group(1).replace(",", "."))
    kws = (settings.get("levels", "keywords", default=[]) if isinstance(settings, Settings)
           else (settings or {}).get("keywords", []))
    for word, value in kws:
        if str(word) in t:
            return float(value)
    m = re.search(r"(\d+(?:[.,]\d+)?)", t)
    return float(m.group(1).replace(",", ".")) if m else None


_SIZE_WORDS = [("xxl", "XXL"), ("2xl", "XXL"), ("xl", "XL"), ("xs", "XS"), ("large", "L"), ("l", "L"),
               ("medium", "M"), ("m", "M"), ("small", "S"), ("s", "S")]


def normalize_size(text: str | None, allowed: list[str] | None = None) -> str | None:
    """Taille t-shirt reconnue ET présente dans la liste autorisée (colonne Notion), sinon None."""
    if not text:
        return None
    allowed = allowed or DEFAULT_SIZES
    toks = set(re.findall(r"[a-z0-9]+", norm(text)))
    found = []
    for raw, label in _SIZE_WORDS:
        if raw in toks and label not in found:
            found.append(label)
    if len(found) != 1:
        return None
    return found[0] if found[0] in allowed else None


def normalize_type(text: str | None, settings: Settings | None = None) -> str | None:
    if not text:
        return None
    cfg = settings.get("types", default={}) if settings else {}
    cfg = cfg or {"tournoi": ["tournoi"], "initiation": ["initiation"], "cocktail": ["cocktail"]}
    t = norm(text)
    for label, key in (("Tournoi", "tournoi"), ("Initiation", "initiation"), ("Cocktail", "cocktail")):
        if any(w in t for w in cfg.get(key, [])):
            return label
    return None


def split_name(full: str | None) -> tuple[str, str]:
    """'Jean Dupont de la Tour' -> ('Jean', 'Dupont de la Tour')."""
    parts = (full or "").split()
    return (parts[0], " ".join(parts[1:])) if parts else ("", "")


def team_name(a_last: str, b_last: str) -> str:
    return " / ".join(sorted([a_last, b_last], key=norm))


# --------------------------------------------------------------------------- Typeform -> personnes
def _person_key(d: dict) -> str:
    return norm(d.get("email")) or norm(f"{d.get('prenom', '')} {d.get('nom', '')}")


def responses_to_people(parsed: list[dict], settings: Settings | None = None) -> list[dict]:
    """Réponses analysées -> personnes (le coéquipier devient une fiche à part entière)."""
    allowed = settings.get("sizes", default=None) if settings else None
    people: dict[str, dict] = {}

    def merge(person: dict):
        key = _person_key(person)
        if not key:
            return
        cur = people.setdefault(key, {})
        for k, v in person.items():
            if v not in (None, ""):
                cur[k] = v  # les réponses plus récentes l'emportent

    for r in sorted(parsed, key=lambda x: x.get("submitted_at", "")):
        main = {"prenom": r.get("prenom") or "", "nom": r.get("nom") or "",
                "email": (r.get("email") or "").strip().lower(), "telephone": r.get("telephone"),
                "company": r.get("company"), "level": r.get("level"),
                "size": normalize_size(r.get("size"), allowed),
                "type": normalize_type(r.get("type"), settings)}
        partner = None
        if r.get("partner_name") or r.get("partner_email"):
            pp, pn = split_name(r.get("partner_name"))
            partner = {"prenom": pp, "nom": pn, "email": (r.get("partner_email") or "").strip().lower(),
                       "company": main["company"], "type": main["type"]}
            team = team_name(main["nom"] or main["prenom"], pn or pp)
            main["team"], partner["team"] = team, team
        merge(main)
        if partner:
            merge(partner)
    return list(people.values())


# --------------------------------------------------------------------------- upsert Notion
def upsert_people(notion, settings: Settings, event, people: list[dict], overwrite: bool = False,
                  existing: list[Participant] | None = None) -> SyncReport:
    """Crée les fiches absentes ; complète les champs vides (ou écrase avec overwrite=True).
    Poule, Position et Statut invitation ne sont jamais modifiés ici."""
    s = settings.schema("participants")
    existing = existing if existing is not None else fetch_participants(notion, settings, event.id)
    by_email = {norm(p.email): p for p in existing if p.email}
    by_name = {norm(p.full_name): p for p in existing if p.full_name}
    report = SyncReport()
    for person in people:
        person = {k: v for k, v in person.items() if k in MANAGED and v not in (None, "")}
        if not person:
            continue
        match = by_email.get(norm(person.get("email"))) or by_name.get(
            norm(f"{person.get('prenom', '')} {person.get('nom', '')}"))
        if match is None:
            if not person.get("prenom"):
                person["prenom"] = person.get("nom") or person.get("email", "?")
            props = s.build({**person, "event": [event.id]})
            page = notion.create_page({"database_id": settings.participants_db}, props)
            new = Participant(id=page["id"], event_ids=[event.id],
                              **{k: v for k, v in person.items() if k in MANAGED})
            if new.email:
                by_email[norm(new.email)] = new
            if new.full_name:
                by_name[norm(new.full_name)] = new
            report.created += 1
            continue
        changes = {}
        for k, v in person.items():
            cur = getattr(match, k)
            if v != cur and (overwrite or not cur):
                changes[k] = v
        if norm_id(event.id) not in [norm_id(i) for i in match.event_ids]:
            changes["event"] = list(match.event_ids) + [event.id]
        if changes:
            notion.update_page(match.id, s.build(changes))
            for k, v in changes.items():
                if k != "event":
                    setattr(match, k, v)
            report.updated += 1
        else:
            report.unchanged += 1
    return report


def sync_participants(notion, settings: Settings, event, tf, overwrite: bool = False) -> SyncReport:
    form_id = settings.typeform_form(event.name)
    if not form_id:
        raise LookupError(f"Aucun formulaire Typeform configuré pour {event.name} "
                          "(section typeform.forms de config.yaml)")
    form = tf.get_form(form_id)
    fields = flatten_fields(form.get("fields"))
    mapping = match_fields(fields, settings.get("typeform", "fields"), settings.get("typeform", "refs"))
    parsed = [parse_response(r, mapping) for r in tf.iter_responses(form_id)]
    people = responses_to_people(parsed, settings)
    report = upsert_people(notion, settings, event, people, overwrite=overwrite)
    for target in ("prenom", "nom", "email"):
        if not mapping.get(target):
            report.warnings.append(f"Aucune question Typeform reconnue pour « {target} » "
                                   "(voir `typeform-fields`)")
    if not parsed:
        report.warnings.append("Aucune réponse complète sur ce formulaire pour l'instant")
    return report


# --------------------------------------------------------------------------- import CSV
_CSV_KEYS = {"prenom": ("prenom", "first name", "firstname"), "nom": ("nom", "name", "last name", "lastname"),
             "email": ("email", "mail", "e-mail", "courriel"),
             "telephone": ("telephone", "phone", "tel", "mobile", "portable"),
             "company": ("entreprise", "societe", "company", "agence"),
             "level": ("level", "niveau"), "size": ("size", "taille"), "team": ("team", "equipe"),
             "type": ("type",)}


def read_people_csv(path: str | Path, settings: Settings | None = None) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    except (csv.Error, IndexError):
        dialect = csv.excel
    allowed = settings.get("sizes", default=None) if settings else None
    people = []
    for row in csv.DictReader(text.splitlines(), dialect=dialect):
        cells = {norm(k): (v or "").strip() for k, v in row.items() if k}
        person = {key: next((cells[a] for a in aliases if cells.get(a)), "") for key, aliases in _CSV_KEYS.items()}
        person["email"] = person["email"].lower()
        person["size"] = normalize_size(person["size"], allowed) or ""
        person["type"] = normalize_type(person["type"], settings) or ""
        if person["prenom"] or person["nom"] or person["email"]:
            people.append(person)
    return people
