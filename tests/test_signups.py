"""Inscriptions du formulaire : solo / équipe, idempotence, poules, étiquettes Word, export tournoi."""
from datetime import date
from pathlib import Path

import pytest
from docx import Document

from padel import importer, store
from padel.config import load_settings
from padel.db import init_db, make_engine
from padel.documents_docx import labels_docx, tournament_csv
from padel.participants import Participant
from padel.signups import parse_signups
from padel.tournament import plan_tournament

CSV = Path(__file__).parent.parent / "examples" / "inscriptions-angers-exemple.csv"


@pytest.fixture()
def env(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    init_db(eng)
    with eng.begin() as cx:
        store.upsert_event(cx, "ANGERS", date=date(2026, 10, 15))
    return eng, load_settings()


def _parts(cx):
    ev = store.get_event(cx, "ANGERS")
    return [Participant(id=str(r["id"]), prenom=r["prenom"], nom=r["nom"], email=r["email"], company=r["company"],
                        team=r["team"], level=r["level"], type=r["type"]) for r in store.list_registrations(cx, ev["id"])]


def test_parse_solo_and_team():
    rows = [{"prenom": "Ana", "nom": "Diaz", "email": "a@x.fr", "niveau": "Confirmé", "enjeux": "Réseau",
             "prenom du coequipier": "Bob", "nom du coequipier": "Ray", "niveau du coequipier": "Débutant"},
            {"prenom": "Seul", "nom": "Joueur", "email": "s@x.fr", "niveau": "Débutant"}]
    out, warns, _ = parse_signups(rows, load_settings())
    assert [len(s["players"]) for s in out] == [2, 1]
    assert out[0]["team"] == "Diaz / Ray" and out[0]["stakes"] == "Réseau"
    assert out[0]["players"][1]["level"] == "Débutant" and out[1]["team"] == ""


def test_import_is_idempotent_and_keeps_details(env):
    eng, s = env
    with eng.begin() as cx:
        r1 = importer.import_signups(cx, CSV.read_text(encoding="utf-8-sig"), "ANGERS", s)
        r2 = importer.import_signups(cx, CSV.read_text(encoding="utf-8-sig"), "ANGERS", s)
        regs = store.list_registrations(cx, store.get_event(cx, "ANGERS")["id"])
    assert r1.created == 15 and r2.created == 0
    assert len(regs) == 15 and sum(1 for r in regs if not r["team"]) == 3
    assert all(r["email"] and r["phone"] and r["stakes"] for r in regs if r["prenom"] in ("Camille", "Hugo"))
    assert any("niveau du coéquipier" in w for w in r1.warnings)


def test_dry_run_writes_nothing(env):
    eng, s = env
    with eng.begin() as cx:
        importer.import_signups(cx, CSV.read_text(encoding="utf-8-sig"), "ANGERS", s, dry_run=True)
        assert store.list_registrations(cx) == []


def test_pools_labels_and_export(env, tmp_path):
    eng, s = env
    with eng.begin() as cx:
        importer.import_signups(cx, CSV.read_text(encoding="utf-8-sig"), "ANGERS", s)
        parts = _parts(cx)
        plan = plan_tournament(parts, s, date(2026, 10, 15))
        mapping = {int(m.id): po.name for po in plan.poules for t in po.teams for m in t.members}
        assert store.set_poules(cx, store.get_event(cx, "ANGERS")["id"], mapping) == 15
    assert len(plan.teams) == 8 and len(plan.poules) == 2
    doc = Document(str(labels_docx(tmp_path / "l.docx", "Brand", "ANGERS", parts)))
    text = "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    assert "Camille DURAND" in text and len(doc.tables) == 1
    lines = tournament_csv(plan).decode("utf-8-sig").splitlines()
    assert lines[0].startswith("Poule;Équipe") and len(lines) == 9


def test_label_pages(env, tmp_path):
    people = [Participant(id=str(i), prenom=f"P{i}", nom=f"N{i:02d}") for i in range(25)]
    doc = Document(str(labels_docx(tmp_path / "l.docx", "B", "E", people)))
    assert len(doc.tables) == 2  # 21 étiquettes par page
