"""Staff : lecture d'un Google Sheet partagé par lien (« Tous les utilisateurs disposant du lien : lecteur »).

Aucune API Google, aucun compte : on lit l'export CSV public du fichier. Le fichier est lu SEULEMENT ; l'app n'y écrit jamais.
"""
from __future__ import annotations

import csv
import io
import re

import requests
from sqlalchemy import and_, delete, insert, select, update

from . import store
from .db import staff
from .participants import normalize_size
from .util import norm, tokens

SHEET_ID = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
GID = re.compile(r"[#&?]gid=(\d+)")


class SheetError(RuntimeError):
    pass


def parse_sheet_url(url: str) -> tuple[str, str | None]:
    m = SHEET_ID.search(url or "")
    if not m:
        raise SheetError("Lien Google Sheet non reconnu (attendu : https://docs.google.com/spreadsheets/d/…).")
    g = GID.search(url)
    return m.group(1), (g.group(1) if g else None)


def export_url(url: str) -> str:
    sid, gid = parse_sheet_url(url)
    return f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv" + (f"&gid={gid}" if gid else "")


def fetch_csv(url: str, session: requests.Session | None = None) -> str:
    """Télécharge l'export CSV de l'onglet ; messages clairs si le fichier n'est pas partagé par lien."""
    sess = session or requests.Session()
    try:
        resp = sess.get(export_url(url), timeout=30, allow_redirects=True)
    except requests.RequestException as exc:
        raise SheetError(f"Google Sheet injoignable : {exc}") from exc
    ctype = resp.headers.get("content-type", "")
    if resp.status_code in (401, 403) or "accounts.google" in resp.url or "text/html" in ctype:
        raise SheetError("Accès refusé : dans Google Sheets, cliquez sur Partager → « Accès général » → « Tous les utilisateurs "
                         "disposant du lien » (Lecteur), puis réessayez.")
    if resp.status_code >= 400:
        raise SheetError(f"Google Sheet : erreur HTTP {resp.status_code}.")
    return resp.content.decode("utf-8-sig", errors="replace")


# --------------------------------------------------------------------------- lecture du tableau
_COLS = {  # champ -> mots (début de mot, sans accent) qui identifient la colonne
    "nom": (["nom", "last", "surname"], ["prenom", "first"]),
    "prenom": (["prenom", "first"], []),
    "shirt_size": (["polo", "taille", "shirt"], []),
    "repas": (["dejeuner", "repas", "midi"], []),
    "role": (["role"], []),
    "hotel": (["hotel", "hebergement"], []),
    "transport": (["transport", "train", "arrivee"], []),
}
_EVENT = (["evenement", "event", "ville", "etape", "tournoi"], ["dejeuner", "repas", "midi", "taille", "polo", "role", "avant"])


def _match(header: str, words: list[str], none: list[str]) -> bool:
    toks = tokens(header)
    return any(t.startswith(w) for t in toks for w in words) and not any(t.startswith(w) for t in toks for w in none)


def parse_staff_csv(text: str) -> tuple[list[dict], list[str]]:
    """CSV -> membres du staff. La ligne d'en-tête est cherchée (bannière/titres au-dessus tolérés)."""
    sample = text[:2000]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    grid = list(csv.reader(io.StringIO(text), delimiter=delim))
    warns: list[str] = []
    head_i = next((i for i, row in enumerate(grid[:15])
                   if any(_match(c, *_COLS["prenom"]) for c in row) and any(_match(c, *_COLS["nom"]) for c in row)), None)
    if head_i is None:
        raise SheetError("En-têtes « Nom » et « Prénom » introuvables dans le fichier (onglet de saisie du staff ?).")
    headers = grid[head_i]
    cols: dict[str, int] = {}
    for field, (words, none) in _COLS.items():
        idx = next((i for i, h in enumerate(headers) if h.strip() and _match(h, words, none) and i not in cols.values()), None)
        if idx is not None:
            cols[field] = idx
    for must, label in (("shirt_size", "Taille polo"), ("repas", "Déjeuner"), ("role", "Rôle")):
        if must not in cols:
            warns.append(f"Colonne « {label} » non reconnue : ce champ restera vide.")
    out = []
    for row in grid[head_i + 1:]:
        item = {f: (row[i].strip() if i < len(row) else "") for f, i in cols.items()}
        if not (item.get("nom") or item.get("prenom")):
            continue
        out.append(_normalize_member(item, warns))
    return out, warns


def _normalize_member(item: dict, warns: list[str]) -> dict:
    raw = item.get("shirt_size", "")
    if raw:
        size = normalize_size(raw)
        if size:
            item["shirt_size"] = size
        else:
            warns.append(f"{item.get('prenom', '')} {item.get('nom', '')} : taille « {raw} » non reconnue (gardée telle quelle).")
    r = norm(item.get("repas", ""))
    if r:
        item["repas"] = "Oui" if r.startswith(("o", "y")) else ("Non" if r.startswith("n") else item["repas"])
    return item


def staff_columns(headers: list[str]) -> dict[str, str]:
    """champ du staff (+ « event ») -> intitulé de question/colonne qui l'alimente."""
    cols: dict[str, str] = {}
    for h in headers:
        if not h.strip():
            continue
        if "event" not in cols and _match(h, *_EVENT):
            cols["event"] = h
            continue
        for field, (words, none) in _COLS.items():
            if field not in cols and _match(h, words, none):
                cols[field] = h
                break
    return cols


def parse_staff_typeform(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Réponses Typeform (lignes {question: réponse}) -> membres du staff, chacun avec sa clé « event » (ville choisie).
    Si une même personne (nom + prénom) répond plusieurs fois pour un événement, la dernière réponse compte."""
    headers: list[str] = []
    for r in rows:
        headers += [k for k in r if k not in headers]
    cols = staff_columns(headers)
    warns: list[str] = []
    if "nom" not in cols and "prenom" not in cols:
        raise SheetError("Questions « Nom » / « Prénom » introuvables dans le formulaire staff.")
    if "event" not in cols:
        warns.append("Aucune question « Événement » reconnue : les réponses ne peuvent pas être rattachées à un événement.")
    latest: dict[tuple[str, str, str], dict] = {}
    for r in rows:   # lignes déjà triées de la plus ancienne à la plus récente
        item = {f: (r.get(h) or "").strip() for f, h in cols.items()}
        if not (item.get("nom") or item.get("prenom")):
            continue
        item = _normalize_member(item, warns)
        latest[(norm(item.get("event")), norm(item.get("nom")), norm(item.get("prenom")))] = item
    return list(latest.values()), warns


def match_event(label: str, event_names: list[str]) -> str | None:
    """« Bordeaux », « BORDEAUX - 19 novembre », « Étape de Saint-Étienne » -> nom d'événement connu (sinon None)."""
    t = norm(label)
    if not t:
        return None
    exact = [n for n in event_names if norm(n) == t]
    if exact:
        return exact[0]
    hits = [n for n in event_names if norm(n) and norm(n) in t]
    return max(hits, key=len) if hits else None


def staff_typeform_diagnostic(form: dict, responses: list[dict], event_names: list[str]) -> list[str]:
    """Structure du formulaire staff et aiguillage des réponses, sans aucune donnée personnelle."""
    from .signups import form_column_names, typeform_rows
    names = form_column_names(form)
    cols = staff_columns(list(names.values()))
    inv = {v: k for k, v in cols.items()}
    lines = [f"{len(responses)} réponse(s) complète(s) lue(s)."]
    for fid, name in names.items():
        lines.append(f"Question « {name} » → {inv.get(name, '(ignorée)')}")
    for f in ("nom", "prenom", "shirt_size", "repas", "role", "event"):
        if f not in cols:
            lines.append(f"⚠ Aucune question reconnue pour « {f} ».")
    ev_col = cols.get("event")
    for i, row in enumerate(typeform_rows(form, responses), 1):
        label = row.get(ev_col, "") if ev_col else ""
        lines.append(f"Réponse {i} : événement « {label or '—'} » → {match_event(label, event_names) or 'NON RECONNU'}")
    return lines


def sync_members(cx, event_id: int, members: list[dict], warns: list[str], dry_run: bool = False,
                 actor: str = "synchro", source: str = "sheet") -> dict:
    """Aligne le staff `source` de l'événement sur `members` : ajoute, met à jour (rôle, polo, déjeuner), retire ceux qui
    n'y figurent plus. Les membres saisis à la main ou venant d'une autre source ne sont jamais supprimés."""
    existing = store.list_staff(cx, event_id)
    by_key = {(norm(r["nom"]), norm(r["prenom"])): r for r in existing}
    seen: set[tuple[str, str]] = set()
    added = updated = 0
    for m in members:
        key = (norm(m.get("nom")), norm(m.get("prenom")))
        if key in seen:
            warns.append(f"« {m.get('prenom', '')} {m.get('nom', '')} » apparaît deux fois dans le fichier : la dernière ligne compte.")
        seen.add(key)
        vals = {k: m.get(k, "") for k in ("nom", "prenom", "role", "shirt_size", "repas", "hotel", "transport") if k in m}
        cur = by_key.get(key)
        if cur is None:
            added += 1
            if not dry_run:
                cx.execute(insert(staff).values(event_id=event_id, source=source, **vals))
                by_key[key] = {"id": None}
        else:
            changed = {k: v for k, v in vals.items() if (cur.get(k) or "") != v}
            if changed:
                updated += 1
                if not dry_run and cur.get("id"):
                    cx.execute(update(staff).where(staff.c.id == cur["id"]).values(source=source, **changed))
    gone = [r for k, r in by_key.items() if k not in seen and r.get("source") == source]
    if gone and not dry_run:
        cx.execute(delete(staff).where(staff.c.id.in_([r["id"] for r in gone])))
    if not dry_run:
        store.log(cx, "staff_sync", event_id, f"{source} : {added} ajouté(s), {updated} mis à jour, {len(gone)} retiré(s)", actor)
    return {"members": len(members), "added": added, "updated": updated, "removed": len(gone), "warnings": warns}


def sync_staff(cx, event_id: int, text: str, dry_run: bool = False, actor: str = "synchro") -> dict:
    """Google Sheet (CSV) -> staff de l'événement (source « sheet »)."""
    members, warns = parse_staff_csv(text)
    return sync_members(cx, event_id, members, warns, dry_run, actor, "sheet")


def sync_staff_typeform(cx, event: dict, client, form_id: str, dry_run: bool = False, cache: dict | None = None,
                        actor: str = "synchro", event_names: list[str] | None = None) -> dict:
    """Formulaire Typeform UNIQUE du staff -> staff de CET événement (les réponses sont aiguillées par la question « Événement »).
    `cache` évite de relire Typeform pour chaque événement pendant un même passage."""
    from .signups import typeform_rows
    key = ("staff_tf", form_id)
    if cache is not None and key in cache:
        rows = cache[key]
    else:
        rows = typeform_rows(client.get_form(form_id), list(client.iter_responses(form_id)))
        if cache is not None:
            cache[key] = rows
    names = event_names or [e["name"] for e in store.list_events(cx)]
    if not rows:
        return {"members": 0, "added": 0, "updated": 0, "removed": 0, "unknown": [], "warnings": ["Aucune réponse complète sur le formulaire staff."]}
    members, warns = parse_staff_typeform(rows)
    mine, unknown = [], set()
    for m in members:
        target = match_event(m.get("event", ""), names)
        if target == event["name"]:
            mine.append(m)
        elif target is None:
            unknown.add((m.get("event") or "").strip() or "(vide)")
    res = sync_members(cx, event["id"], mine, warns, dry_run, actor, "typeform")
    res["unknown"] = sorted(unknown)
    return res
