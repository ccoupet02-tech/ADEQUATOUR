"""Constitution des équipes, poules équilibrées (serpentin), round-robin et planning des terrains."""
from __future__ import annotations

import math
import statistics
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Settings
from .participants import Participant, active_participants, parse_level, team_name
from .util import norm


@dataclass(eq=False)
class Team:
    uid: int
    name: str
    members: list[Participant]
    level: float = 0.0
    incomplete: bool = False


@dataclass
class Poule:
    name: str
    teams: list[Team] = field(default_factory=list)


@dataclass(eq=False)
class Match:
    poule: str
    round: int
    team_a: Team
    team_b: Team
    slot: int = -1
    court: int = 0
    start: datetime | None = None


@dataclass
class Plan:
    teams: list[Team]
    poules: list[Poule]
    matches: list[Match]
    warnings: list[str]
    start: datetime
    minutes: int

    @property
    def end(self) -> datetime:
        last = max((m.slot for m in self.matches), default=-1) + 1
        return self.start + timedelta(minutes=self.minutes * last)


def select_players(participants: list[Participant], settings: Settings) -> tuple[list[Participant], list[str]]:
    cfg = settings.get("tournament", default={})
    include = {norm(t) for t in cfg.get("include_types", ["Tournoi"])}
    untyped_ok = cfg.get("include_untyped", True)
    chosen, untyped = [], 0
    for p in active_participants(participants, settings):
        if not p.type:
            if untyped_ok:
                chosen.append(p)
                untyped += 1
        elif norm(p.type) in include:
            chosen.append(p)
    warnings = [f"{untyped} participant(s) sans « Type » comptés comme tournoi"] if untyped else []
    return chosen, warnings


def build_teams(players: list[Participant], settings: Settings) -> tuple[list[Team], list[str]]:
    """Équipes déclarées (même valeur de « Team ») + appariement des solos par niveau voisin."""
    warnings: list[str] = []
    lvl = lambda p: parse_level(p.level, settings)  # noqa: E731
    known = [v for v in (lvl(p) for p in players) if v is not None]
    median = statistics.median(known) if known else 0.0
    groups: dict[str, list[Participant]] = {}
    solos: list[Participant] = []
    for p in players:
        if p.team.strip():
            groups.setdefault(norm(p.team), []).append(p)
        else:
            solos.append(p)
    teams: list[Team] = []
    for members in groups.values():
        if len(members) >= 2:
            if len(members) > 2:
                warnings.append(f"Équipe « {members[0].team} » : {len(members)} joueurs (2 attendus)")
            teams.append(Team(0, members[0].team, members))
        else:
            warnings.append(f"Équipe « {members[0].team} » incomplète : {members[0].full_name} sera apparié(e)")
            solos.append(members[0])
    solos.sort(key=lambda p: (lvl(p) if lvl(p) is not None else median, norm(p.full_name)))
    for i in range(0, len(solos) - 1, 2):
        a, b = solos[i], solos[i + 1]
        teams.append(Team(0, team_name(a.nom or a.prenom, b.nom or b.prenom), [a, b]))
    if len(solos) % 2:
        lone = solos[-1]
        warnings.append(f"Joueur sans coéquipier : {lone.full_name} (équipe incomplète)")
        teams.append(Team(0, lone.full_name or "?", [lone], incomplete=True))
    seen: dict[str, int] = {}
    for i, t in enumerate(teams):
        t.uid = i
        k = norm(t.name)
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > 1:
            t.name = f"{t.name} ({seen[k]})"
        vals = [v for v in (lvl(p) for p in t.members) if v is not None]
        t.level = statistics.mean(vals) if vals else median
    return teams, warnings


def poule_name(i: int) -> str:
    return string.ascii_uppercase[i] if i < 26 else f"P{i + 1}"


def make_poules(teams: list[Team], poule_size: int = 4) -> list[Poule]:
    """Serpentin : les têtes de série sont réparties, chaque poule reste de force comparable."""
    n = len(teams)
    if n == 0:
        return []
    k = max(1, math.ceil(n / poule_size))
    while k > 1 and n // k < 3:
        k -= 1
    poules = [Poule(poule_name(i)) for i in range(k)]
    for i, team in enumerate(sorted(teams, key=lambda t: (-t.level, norm(t.name)))):
        row, col = divmod(i, k)
        poules[col if row % 2 == 0 else k - 1 - col].teams.append(team)
    return poules


def round_robin(teams: list[Team]) -> list[list[tuple[Team, Team]]]:
    """Méthode du cercle : chaque équipe rencontre toutes les autres une fois."""
    ids: list[Team | None] = list(teams)
    if len(ids) % 2:
        ids.append(None)
    n, rounds = len(ids), []
    for _ in range(max(n - 1, 0)):
        rounds.append([(ids[i], ids[n - 1 - i]) for i in range(n // 2)
                       if ids[i] is not None and ids[n - 1 - i] is not None])
        ids = [ids[0]] + [ids[-1]] + ids[1:-1]
    return rounds


def schedule_matches(poules: list[Poule], courts: int, start: datetime, minutes: int,
                     rest_slots: int = 1) -> list[Match]:
    per_poule = [(p, round_robin(p.teams)) for p in poules]
    pending: list[Match] = []
    for r in range(max((len(rs) for _, rs in per_poule), default=0)):
        for p, rs in per_poule:
            if r < len(rs):
                pending += [Match(p.name, r + 1, a, b) for a, b in rs[r]]
    out, last, slot = [], set(), 0
    while pending:
        picked, busy = [], set()
        for relax in ((False, True) if rest_slots else (True,)):
            for m in pending:
                if len(picked) >= courts:
                    break
                ids = {m.team_a.uid, m.team_b.uid}
                if ids & busy or (not relax and ids & last) or m in picked:
                    continue
                picked.append(m)
                busy |= ids
            if picked:
                break
        for court, m in enumerate(picked, 1):
            m.slot, m.court, m.start = slot, court, start + timedelta(minutes=minutes * slot)
            pending.remove(m)
            out.append(m)
        last, slot = busy, slot + 1
    return out


def plan_tournament(participants: list[Participant], settings: Settings, event_date,
                    poule_size: int | None = None, courts: int | None = None,
                    minutes: int | None = None, start_time: str | None = None) -> Plan:
    cfg = settings.get("tournament", default={})
    players, warnings = select_players(participants, settings)
    teams, w2 = build_teams(players, settings)
    poules = make_poules(teams, poule_size or cfg.get("poule_size", 4))
    hh, mm = (start_time or cfg.get("start_time", "09:30")).split(":")
    start = datetime.combine(event_date, datetime.min.time()).replace(hour=int(hh), minute=int(mm))
    mins = minutes or cfg.get("match_minutes", 20)
    matches = schedule_matches(poules, courts or cfg.get("courts", 4), start, mins, cfg.get("rest_slots", 1))
    return Plan(teams, poules, matches, warnings + w2, start, mins)


def write_poules_to_notion(notion, settings: Settings, plan: Plan) -> int:
    """Écrit Poule (« Poule A »…) et Position (rang de tête de série) sur chaque fiche."""
    s = settings.schema("participants")
    n = 0
    for poule in plan.poules:
        for rank, team in enumerate(sorted(poule.teams, key=lambda t: -t.level), 1):
            for member in team.members:
                if member.id:
                    notion.update_page(member.id, s.build({"poule": f"Poule {poule.name}",
                                                           "position": str(rank)}))
                    n += 1
    return n
