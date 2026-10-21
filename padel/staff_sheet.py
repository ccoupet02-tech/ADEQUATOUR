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
    "nom": (["nom"], ["prenom"]),
    "prenom": (["prenom"], []),
    "shirt_size": (["polo", "taille", "shirt"], []),
    "repas": (["dejeuner", "repas", "midi"], []),
    "role": (["role"], []),
    "hotel": (["hotel", "hebergement"], []),
    "transport": (["transport", "train", "arrivee"], []),
}


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
        out.append(item)
    return out, warns


def sync_staff(cx, event_id: int, text: str, dry_run: bool = False, actor: str = "synchro") -> dict:
    """Aligne le staff « sheet » de l'événement sur le fichier : ajoute, met à jour (rôle, polo, déjeuner), retire ceux
    qui n'y figurent plus. Les membres saisis à la main dans le dashboard ne sont jamais supprimés."""
    members, warns = parse_staff_csv(text)
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
                cx.execute(insert(staff).values(event_id=event_id, source="sheet", **vals))
                by_key[key] = {"id": None}
        else:
            changed = {k: v for k, v in vals.items() if (cur.get(k) or "") != v}
            if changed:
                updated += 1
                if not dry_run and cur.get("id"):
                    cx.execute(update(staff).where(staff.c.id == cur["id"]).values(source="sheet", **changed))
    gone = [r for k, r in by_key.items() if k not in seen and r.get("source") == "sheet"]
    if gone and not dry_run:
        cx.execute(delete(staff).where(staff.c.id.in_([r["id"] for r in gone])))
    if not dry_run:
        store.log(cx, "staff_sync", event_id, f"{added} ajouté(s), {updated} mis à jour, {len(gone)} retiré(s)", actor)
    return {"members": len(members), "added": added, "updated": updated, "removed": len(gone), "warnings": warns}
