"""Logistique : t-shirts à commander, repas, hôtel/transport du staff."""
from __future__ import annotations

import csv
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .participants import DEFAULT_SIZES, Participant, active_participants, normalize_size
from .util import norm


@dataclass
class StaffMember:
    name: str = ""
    prenom: str = ""
    role: str = ""
    shirt: str = ""
    hotel: str = ""
    transport: str = ""
    meal: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.prenom} {self.name}".strip()


def read_staff(notion, settings: Settings, event) -> list[StaffMember]:
    db = notion.find_child_database(event.id, "Staff")
    if not db:
        return []
    s = settings.schema("staff")
    out = []
    for page in notion.query_database(db):
        out.append(StaffMember(*(s.read(page, k) or "" for k in
                                 ("name", "prenom", "role", "shirt", "hotel", "transport", "meal"))))
    return [m for m in out if m.full_name]


def _yes(v: str) -> bool:
    return norm(v) == "oui"


def logistics_report(settings: Settings, event, participants: list[Participant],
                     staff: list[StaffMember]) -> dict:
    sizes = settings.get("sizes", default=DEFAULT_SIZES)
    guests = active_participants(participants, settings)
    # hypothèse : les invités « Cocktail » seuls n'ont ni repas du midi ni t-shirt
    players = [p for p in guests if norm(p.type) != "cocktail"]
    margin = settings.get("logistics", "tshirt_margin", default=0.1)
    t_players = Counter(normalize_size(p.size, sizes) or "?" for p in players)
    t_staff = Counter(normalize_size(m.shirt, sizes + ["XS"]) or "?" for m in staff)
    total = t_players + t_staff
    order = {s: math.ceil(total[s] * (1 + margin)) for s in sizes + ["XS"] if total[s]}
    return {
        "event": event.name, "date": event.date, "sizes": sizes + ["XS"],
        "participants": {"fiches": len(participants), "actifs": len(guests),
                         "par_type": dict(Counter(p.type or "sans type" for p in guests)),
                         "par_statut": dict(Counter(p.invite_status or "non invité" for p in participants))},
        "staff": {"total": len(staff), "hotel": sum(_yes(m.hotel) for m in staff),
                  "transport": dict(Counter(m.transport or "non renseigné" for m in staff)),
                  "repas": sum(_yes(m.meal) for m in staff),
                  "par_role": dict(Counter(m.role or "sans rôle" for m in staff))},
        "repas_total": len(players) + sum(_yes(m.meal) for m in staff),
        "tshirts": {"participants": dict(t_players), "staff": dict(t_staff), "total": dict(total),
                    "a_commander": order, "marge": margin,
                    "taille_manquante": t_players.get("?", 0) + t_staff.get("?", 0)},
    }


def write_logistics(out_dir: Path, report: dict) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    t, p, s = report["tshirts"], report["participants"], report["staff"]
    lines = [f"# Logistique - {report['event']}", "",
             f"- Fiches participants : {p['fiches']} (actives : {p['actifs']})",
             f"- Répartition par type : {p['par_type']}", f"- Staff : {s['total']} personnes",
             f"- Repas du midi à réserver : **{report['repas_total']}** "
             f"(joueurs hors cocktail + staff « Repas = Oui »)",
             f"- Hôtels staff : {s['hotel']} · Transport : {s['transport']}", "",
             f"## T-shirts (marge {int(t['marge'] * 100)} %)", "",
             "| Taille | Participants | Staff | Total | À commander |", "|---|---|---|---|---|"]
    for size in report["sizes"]:
        if t["total"].get(size):
            lines.append(f"| {size} | {t['participants'].get(size, 0)} | {t['staff'].get(size, 0)} | "
                         f"{t['total'][size]} | {t['a_commander'][size]} |")
    if t["taille_manquante"]:
        lines += ["", f"⚠️ {t['taille_manquante']} personne(s) sans taille renseignée (non comptées ci-dessus)."]
    md = out_dir / "logistics.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cs = out_dir / "tshirts.csv"
    with open(cs, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["taille", "participants", "staff", "total", "a_commander"])
        for size in report["sizes"]:
            if t["total"].get(size):
                w.writerow([size, t["participants"].get(size, 0), t["staff"].get(size, 0),
                            t["total"][size], t["a_commander"][size]])
    return [md, cs]
