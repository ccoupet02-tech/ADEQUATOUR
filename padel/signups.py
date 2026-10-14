"""Inscriptions issues du formulaire (export CSV du Google Sheet / Typeform).

Une ligne = un inscrit seul, ou deux joueurs qui forment une équipe (le coéquipier est décrit dans les colonnes
« coéquipier / partenaire / binôme »). Les colonnes sont reconnues par leur intitulé (config.yaml > typeform.fields),
donc l'ordre et le libellé exact importent peu.
"""
from __future__ import annotations

from datetime import datetime

from .config import Settings
from .participants import normalize_size, normalize_type, parse_level, split_name, team_name
from .typeform import MULTI, title_matches
from .util import norm


def map_headers(headers: list[str], matchers: dict) -> dict[str, list[str]]:
    """clé logique -> intitulés de colonnes correspondants (partenaire testé avant le joueur principal)."""
    mapping: dict[str, list[str]] = {k: [] for k in matchers}
    for h in headers:
        for key, spec in matchers.items():
            if title_matches(h, spec):
                mapping[key].append(h)
                break
    return mapping


def _val(row: dict, cols: list[str], join: bool = False) -> str:
    vals = [row[c].strip() for c in cols if row.get(c, "").strip()]
    return (" ".join(vals) if join else vals[0]) if vals else ""


def _when(text: str) -> datetime | None:
    t = (text or "").strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def parse_signups(rows: list[dict], settings: Settings | None = None) -> tuple[list[dict], list[str], dict]:
    """Lignes du CSV (clés = intitulés normalisés) -> inscriptions.

    Retourne (inscriptions, avertissements, colonnes reconnues). Une inscription =
    {"line", "players": [joueur1, joueur2?], "team", "stakes", "registered_at"}."""
    matchers = settings.get("typeform", "fields") if settings else {}
    sizes = settings.get("sizes", default=None) if settings else None
    warnings: list[str] = []
    if not rows:
        return [], ["Le fichier ne contient aucune ligne."], {}
    mapping = map_headers(list(rows[0].keys()), matchers)
    for needed, label in (("prenom", "prénom"), ("nom", "nom"), ("email", "e-mail")):
        if not mapping.get(needed):
            warnings.append(f"Colonne « {label} » non reconnue : vérifiez l'intitulé dans le fichier.")
    out = []
    for i, row in enumerate(rows, start=2):
        g = lambda k, join=False: _val(row, mapping.get(k, []), join or k in MULTI)  # noqa: E731
        main = {"prenom": g("prenom"), "nom": g("nom"), "email": g("email").lower(), "phone": g("telephone"),
                "company": g("company"), "level": g("level"),
                "size": normalize_size(g("size"), sizes) or "",
                "type": normalize_type(g("type"), settings) or g("type")}
        if not (main["prenom"] or main["nom"] or main["email"]):
            continue
        players = [main]
        team = ""
        pname, pmail = g("partner_name"), g("partner_email").lower()
        if pname or pmail:
            pp, pn = split_name(pname)
            partner = {"prenom": pp, "nom": pn, "email": pmail, "phone": g("partner_phone"),
                       "company": g("partner_company") or main["company"], "level": g("partner_level"),
                       "size": "", "type": main["type"]}
            if not partner["level"] and main["level"]:
                partner["level"] = main["level"]
                warnings.append(f"ligne {i} : niveau du coéquipier non indiqué, celui de {main['prenom']} "
                                f"{main['nom']} est repris.")
            if not pn and pp:
                warnings.append(f"ligne {i} : nom du coéquipier incomplet (« {pname} »).")
            team = team_name(main["nom"] or main["prenom"], pn or pp)
            players.append(partner)
        for p in players:
            lv = p["level"]
            if lv and parse_level(lv, settings) is None:
                warnings.append(f"ligne {i} : niveau « {lv} » non reconnu ({p['prenom']} {p['nom']}).")
            if not p["email"] and p is main:
                warnings.append(f"ligne {i} : pas d'e-mail pour {p['prenom']} {p['nom']}.")
        out.append({"line": i, "players": players, "team": team, "stakes": g("stakes"),
                    "registered_at": _when(g("submitted"))})
    seen: dict[str, int] = {}
    for s in out:  # deux équipes de même nom (homonymes) : suffixe pour ne pas les confondre
        if s["team"]:
            k = norm(s["team"])
            seen[k] = seen.get(k, 0) + 1
            if seen[k] > 1:
                s["team"] = f"{s['team']} ({seen[k]})"
    return out, warnings, mapping


def typeform_rows(form: dict, responses: list[dict]) -> list[dict]:
    """Réponses Typeform -> lignes {intitulé de la question normalisé: réponse}, au même format que l'export CSV."""
    from .typeform import answer_value, flatten_fields
    titles = {f["id"]: norm(f["title"]) for f in flatten_fields(form.get("fields"))}
    rows = []
    for r in sorted(responses, key=lambda x: x.get("submitted_at", "")):
        row = {"submitted_at": (r.get("submitted_at") or "").replace("T", " ").replace("Z", "")}
        for a in r.get("answers", []):
            t = titles.get(a["field"]["id"])
            v = answer_value(a)
            if t and v not in (None, ""):
                row[t] = str(v).strip()
        rows.append(row)
    return rows
