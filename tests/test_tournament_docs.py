"""Poules, planning, PDF, logistique, CLI."""
from collections import Counter
from datetime import date, datetime

import pytest
from pypdf import PdfReader

from padel import documents
from padel.cli import main
from padel.logistics import StaffMember, logistics_report, write_logistics
from padel.participants import Participant
from padel.tournament import Poule, Team, make_poules, plan_tournament, round_robin, schedule_matches


def _players(n, with_team=True):
    out = []
    for i in range(n):
        out.append(Participant(id=f"p{i}", prenom=f"P{i}", nom=f"N{i}", email=f"p{i}@x.fr",
                               level=str(1 + i % 4), size=["S", "M", "L", "XL"][i % 4], type="Tournoi",
                               team=f"T{i // 2}" if with_team else ""))
    return out


def _teams(n):
    return [Team(i, f"E{i}", [], level=float(i % 4)) for i in range(n)]


@pytest.mark.parametrize("n", [3, 4, 5, 6, 7])
def test_round_robin_chaque_paire_une_seule_fois(n):
    teams = _teams(n)
    pairs = [frozenset((a.uid, b.uid)) for rnd in round_robin(teams) for a, b in rnd]
    assert len(pairs) == len(set(pairs)) == n * (n - 1) // 2
    for rnd in round_robin(teams):  # une équipe joue au plus une fois par tour
        ids = [t.uid for pair in rnd for t in pair]
        assert len(ids) == len(set(ids))


def test_poules_equilibrees_en_serpentin():
    teams = [Team(i, f"E{i}", [], level=float(8 - i)) for i in range(8)]   # niveaux 8..1
    poules = make_poules(teams, 4)
    assert [len(p.teams) for p in poules] == [4, 4]
    sums = [sum(t.level for t in p.teams) for p in poules]
    assert abs(sums[0] - sums[1]) <= 1                                      # forces comparables
    assert len(make_poules(_teams(5), 4)) == 1                              # pas de poule de 2
    assert make_poules([], 4) == []


def test_planning_respecte_terrains_et_repos():
    poules = make_poules(_teams(8), 4)
    start = datetime(2026, 10, 1, 9, 30)
    matches = schedule_matches(poules, courts=2, start=start, minutes=20, rest_slots=1)
    assert len(matches) == 12
    for slot in {m.slot for m in matches}:
        same = [m for m in matches if m.slot == slot]
        assert len(same) <= 2 and len({m.court for m in same}) == len(same)
        ids = [t.uid for m in same for t in (m.team_a, m.team_b)]
        assert len(ids) == len(set(ids))                                    # personne sur 2 terrains à la fois
    assert min(m.start for m in matches) == start


def test_plan_complet_et_solo(settings):
    plan = plan_tournament(_players(10), settings, date(2026, 10, 1))
    assert len(plan.teams) == 5 and sum(len(p.teams) for p in plan.poules) == 5
    solos = _players(5, with_team=False)                                    # nombre impair, aucune équipe
    plan = plan_tournament(solos, settings, date(2026, 10, 1))
    assert len(plan.teams) == 3 and any("sans coéquipier" in w for w in plan.warnings)
    refus = _players(4)
    refus[0].invite_status = "Refusée"
    assert len(plan_tournament(refus, settings, date(2026, 10, 1)).teams) <= 2   # refusés exclus


def test_les_initiations_ne_jouent_pas_le_tournoi(settings):
    ps = _players(4)
    ps[0].type = ps[1].type = "Initiation"
    plan = plan_tournament(ps, settings, date(2026, 10, 1))
    assert sum(len(t.members) for t in plan.teams) == 2


def test_pdf_valides_et_accents(tmp_path, settings):
    ps = _players(10)
    ps[0].prenom, ps[0].nom, ps[0].company = "Éloïse", "Lefèvre-Çelik", "Société Générale d'Électricité"
    plan = plan_tournament(ps, settings, date(2026, 10, 1))
    brand = settings.brand
    files = {
        "planning": documents.planning_pdf(tmp_path / "a.pdf", brand, "DIJON", date(2026, 10, 1), plan),
        "poules": documents.poules_pdf(tmp_path / "b.pdf", brand, "DIJON", plan),
        "arbitres": documents.referee_pdf(tmp_path / "c.pdf", brand, "DIJON", plan),
        "diplomes": documents.diplomas_pdf(tmp_path / "d.pdf", brand, "DIJON", date(2026, 10, 1), ps),
        "etiquettes": documents.labels_pdf(tmp_path / "e.pdf", brand, "DIJON", ps)}
    assert len(PdfReader(str(files["diplomes"])).pages) == 10               # un diplôme par personne
    assert len(PdfReader(str(files["arbitres"])).pages) == len({m.court for m in plan.matches})
    assert len(PdfReader(str(files["poules"])).pages) == len(plan.poules)
    assert "Lefèvre" in PdfReader(str(files["diplomes"])).pages[0].extract_text() or \
           "LEFÈVRE" in PdfReader(str(files["diplomes"])).pages[0].extract_text()
    many = [Participant(prenom=f"P{i}", nom=f"N{i}") for i in range(50)]    # 21 étiquettes par page
    assert len(PdfReader(str(documents.labels_pdf(tmp_path / "f.pdf", brand, "X", many))).pages) == 3
    for fn in (documents.diplomas_pdf, ):                                   # liste vide : PDF valide quand même
        assert len(PdfReader(str(fn(tmp_path / "g.pdf", brand, "X", None, []))).pages) == 1


def test_logistique_tshirts_avec_marge(settings, event, tmp_path):
    ps = _players(10)
    ps[0].size = ""                                                          # taille manquante
    ps[1].type = "Cocktail"                                                  # exclu des t-shirts
    staff = [StaffMember(name="Dupont", prenom="Jo", shirt="M", hotel="Oui", meal="Oui", transport="Train"),
             StaffMember(name="Durand", prenom="Lu", shirt="XXL", hotel="Non", meal="Oui", transport="Voiture")]
    rep = logistics_report(settings, event, ps, staff)
    t = rep["tshirts"]
    assert t["taille_manquante"] == 1
    assert sum(t["participants"].values()) == 9 and Counter(t["staff"]) == Counter({"M": 1, "XXL": 1})
    assert all(t["a_commander"][s] >= t["total"][s] for s in t["a_commander"])
    assert t["a_commander"]["XXL"] == 2                                      # ceil(1 * 1.1)
    assert rep["repas_total"] == 9 + 2 and rep["staff"]["hotel"] == 1
    md, csv_path = write_logistics(tmp_path, rep)
    assert "À commander" in md.read_text(encoding="utf-8") and csv_path.exists()


def test_cli_refuse_send_sans_apply(capsys):
    assert main(["daily", "--send"]) == 2
    assert "--send exige --apply" in capsys.readouterr().err


# --------------------------------------------------------------------------- CLI de bout en bout
from conftest import ENV  # noqa: E402
from padel.config import Settings  # noqa: E402
from padel.participants import fetch_participants, upsert_people  # noqa: E402
from padel.tasks import fetch_tasks  # noqa: E402


@pytest.fixture
def cli(monkeypatch, tmp_path, notion):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PADEL_OUT_DIR", str(tmp_path / "out"))

    def fake_client(self, dry_run=False):
        notion.dry_run = dry_run
        return notion

    monkeypatch.setattr(Settings, "notion_client", fake_client)
    return notion


def test_cli_simulation_n_ecrit_rien(cli, settings, event, capsys):
    before = cli.writes
    assert main(["sync-tasks", "--event", "DIJON", "--today", "2026-09-20"]) == 0
    assert cli.writes == before                              # aucune écriture sans --apply
    assert fetch_tasks(cli, settings, event.id) == []
    out = capsys.readouterr().out
    assert "SIMULATION" in out and f"{len(settings.templates())} tâche(s) créée(s)" in out


def test_cli_create_event_puis_list_events(cli, settings, capsys):
    assert main(["create-event", "ANGERS", "2026-10-15", "--venue", "Padel Angers", "--apply"]) == 0
    assert main(["list-events", "--today", "2026-10-14"]) == 0
    out = capsys.readouterr().out
    assert "ANGERS" in out and "15/10/2026" in out and "J+1" in out
    ev = next(p for p in cli.pages.values() if p["parent"].get("database_id") == "db-events")
    assert len(fetch_tasks(cli, settings, ev["id"])) == len(settings.templates())
    assert main(["create-event", "angers", "2026-10-15", "--apply"]) == 2     # doublon refusé


def test_cli_sync_tasks_all_ignore_les_evenements_passes(cli, settings, event, capsys):
    assert main(["sync-tasks", "--all", "--apply", "--today", "2026-12-01"]) == 0   # DIJON (1er oct) est passé
    assert "ignoré (événement passé" in capsys.readouterr().out
    assert fetch_tasks(cli, settings, event.id) == []


def test_cli_documents_ecrit_les_pdf_et_les_poules(cli, settings, event, tmp_path, capsys):
    upsert_people(cli, settings, event, [{"prenom": f"P{i}", "nom": f"N{i}", "email": f"p{i}@x.fr",
                                          "level": str(1 + i % 4), "team": f"T{i // 2}"} for i in range(8)])
    assert main(["documents", "--event", "DIJON", "--apply", "--write-poules"]) == 0
    out = tmp_path / "out" / "dijon"
    for name in ("planning", "poules", "arbitres", "diplomes", "etiquettes"):
        assert len(PdfReader(str(out / f"{name}.pdf")).pages) >= 1
    assert {p.poule for p in fetch_participants(cli, settings, event.id)} <= {"Poule A", "Poule B"}
    assert all(p.position for p in fetch_participants(cli, settings, event.id))


def test_cli_mail_par_defaut_ecrit_des_eml_sans_toucher_notion(cli, settings, event, tmp_path, capsys):
    upsert_people(cli, settings, event, [{"prenom": "Alice", "nom": "Martin", "email": "a@x.fr"}])
    assert main(["mail", "invitation", "--event", "DIJON"]) == 0
    assert len(list((tmp_path / "out" / "dijon" / "mails").glob("*.eml"))) == 1
    assert fetch_participants(cli, settings, event.id)[0].invite_status == ""


def test_cli_daily(cli, settings, event, capsys):
    assert main(["daily", "--apply", "--today", "2026-09-20"]) == 0
    out = capsys.readouterr().out
    assert "DIJON" in out and "en retard" in out and "aucun canal" in out    # rien envoyé sans --send
    assert len(fetch_tasks(cli, settings, event.id)) == len(settings.templates())
