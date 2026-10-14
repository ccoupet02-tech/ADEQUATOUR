"""Base de données, imports Notion, moteur d'automatisation (SQLite en mémoire, aucun réseau)."""
from datetime import date, datetime

import pytest
from sqlalchemy import select

from padel import engine as eng, importer, store
from padel.config import load_settings
from padel.db import init_db, make_engine, normalize_url, communications

TODAY = date(2026, 10, 14)


@pytest.fixture()
def env():
    e = make_engine("sqlite:///:memory:")
    init_db(e)
    return e, load_settings(env={}, use_dotenv=False)


def test_normalize_neon_url():
    url, args = normalize_url("postgresql://u:p@ep-x.eu-west.aws.neon.tech/neondb?sslmode=require&channel_binding=require")
    assert url == "postgresql+pg8000://u:p@ep-x.eu-west.aws.neon.tech/neondb"
    assert args == {"ssl_context": True}
    assert normalize_url("sqlite:///x.db") == ("sqlite:///x.db", {})


def test_seed_is_idempotent_and_complete(env):
    e, s = env
    with e.begin() as cx:
        assert importer.seed_events(cx, s).created == 14
    with e.begin() as cx:
        assert importer.seed_events(cx, s).created == 0
        bordeaux = store.get_event(cx, "bordeaux")
        tl = store.list_tasks(cx, bordeaux["id"])
        assert len(tl) == 22 and tl[0]["deadline"] == date(2026, 9, 20) and tl[0]["name"] == "Demande devis"
        classes = [store.classify(t["status"], t["deadline"], TODAY) for t in tl]
        assert classes.count("late") == 2 and classes.count("soon") == 2


def test_parse_dates_and_relations():
    p = importer.parse_any_date
    assert p("2026-11-19") == p("19/11/2026") == p("19 novembre 2026") == p("November 19, 2026") == date(2026, 11, 19)
    assert p("") is None
    assert importer.relation_name("DIJON (https://www.notion.so/abc), RENNES (https://x)") == "DIJON"


TASKS_CSV = """Tache,Catégorie,Offset,Statut,Event
Demande devis,Lieu,60,In progress,DIJON (https://www.notion.so/x)
Demande devis (1),Lieu,60,Done,DIJON (https://www.notion.so/x)
Invitation (1),Mail,27,Not started,DIJON (https://www.notion.so/x)
,,,Not started,
Invitation,Mail,27,Not started,INCONNU (https://x)
"""


def test_import_tasks_merges_duplicates_keeping_most_advanced(env):
    e, s = env
    with e.begin() as cx:
        store.upsert_event(cx, "DIJON", date=date(2026, 12, 1))
        rep = importer.import_tasks(cx, TASKS_CSV)
        assert rep.created == 2 and rep.warnings  # événement inconnu signalé
        names = {t["name"]: t["status"] for t in store.list_tasks(cx)}
        assert names == {"Demande devis": "done", "Invitation": "todo"}
        assert importer.import_tasks(cx, TASKS_CSV).created == 0  # rejouable


PARTS_CSV = """Prenom,Nom,Email,Entreprise,Team,Size,Type,Statut invitation,Phone,Tournoi
Philippe,MICHAUD,m@x.fr,SATEL,SATEL ESSILOR,M,Tournoi,Acceptée,'+33620142374,PERIGUEUX (https://n/x)
Damien,MERCE,,ESSILOR,SATEL ESSILOR,XXXL,Tournoi,Not started,,PERIGUEUX (https://n/x)
Yves,CHATENET,y@e3r.fr,E3R,,,Cocktail,Acceptée,,PERIGUEUX (https://n/x)
Ghost,Nobody,g@x.fr,,,,Tournoi,Not started,,NULLEPART
"""


def test_import_participants(env):
    e, s = env
    with e.begin() as cx:
        store.upsert_event(cx, "PERIGUEUX", date=date(2026, 7, 9))
        rep = importer.import_participants(cx, PARTS_CSV, settings=s)
        assert (rep.created, rep.skipped) == (3, 1)
        assert any("taille" in w for w in rep.warnings)
        rows = store.list_registrations(cx)
        m = next(r for r in rows if r["nom"] == "MICHAUD")
        assert m["phone"] == "+33620142374" and m["invite_status"] == "accepte" and m["size"] == "M"
        assert importer.import_participants(cx, PARTS_CSV, settings=s).created == 0


def _setup_invites(e, s, event_date=date(2026, 10, 30)):  # invitation due le 03/10, relance le 10/10
    with e.begin() as cx:
        eid, _ = store.upsert_event(cx, "BORDEAUX", date=event_date, venue="3D Padel")
        for prenom, nom, mail, typ in [("Ana", "Lopez", "ana@x.fr", "Tournoi"), ("Bob", "Sans", "", "Tournoi"),
                                       ("Cy", "Cocktail", "cy@x.fr", "Cocktail")]:
            pid, _ = store.upsert_person(cx, prenom=prenom, nom=nom, email=mail)
            store.upsert_registration(cx, eid, pid, type=typ)
    return eid


def test_engine_prepares_once_and_flags_missing_email(env):
    e, s = env
    _setup_invites(e, s)
    dry = eng.run(e, s, TODAY, dry_run=True)
    assert dry.invitations == 1 and dry.pendings == 1
    with e.connect() as cx:
        assert cx.execute(select(communications)).first() is None  # simulation : rien d'écrit
    first = eng.run(e, s, TODAY)
    again = eng.run(e, s, TODAY)
    assert (first.invitations, first.pendings) == (1, 1)
    assert (again.invitations, again.pendings, again.skipped_existing) == (0, 0, 1)
    with e.connect() as cx:
        rows = cx.execute(select(communications)).mappings().all()
        assert len(rows) == 1 and rows[0]["status"] == "preview" and rows[0]["to_addr"] == "ana@x.fr"
        assert "Bonjour Ana" in rows[0]["body"] and "BORDEAUX" in rows[0]["subject"]


def test_engine_waits_until_invitation_is_due(env):
    e, s = env
    _setup_invites(e, s, event_date=date(2027, 3, 1))  # invitation due le 02/02/2027
    assert eng.run(e, s, TODAY).invitations == 0


def test_mark_sent_then_relance_then_answer_cancels_it(env):
    e, s = env
    eid = _setup_invites(e, s)
    eng.run(e, s, TODAY)
    with e.begin() as cx:
        comm = cx.execute(select(communications)).mappings().first()
        store.mark_sent(cx, comm["id"])
        reg = next(r for r in store.list_registrations(cx, eid) if r["nom"] == "Lopez")
        assert reg["invite_status"] == "invite" and reg["invited_at"] is not None
    assert eng.run(e, s, TODAY).relances == 0                        # trop tôt (7 jours)
    later = date.today()
    with e.begin() as cx:  # on simule une invitation envoyée il y a 10 jours
        from padel.db import registrations
        from sqlalchemy import update
        cx.execute(update(registrations).values(invited_at=datetime(2026, 10, 1)))
    assert eng.run(e, s, TODAY).relances == 1
    assert eng.run(e, s, TODAY).relances == 0                        # idempotent
    with e.begin() as cx:
        reg = next(r for r in store.list_registrations(cx, eid) if r["nom"] == "Lopez")
        store.set_invite_status(cx, reg["id"], "accepte")
        rel = cx.execute(select(communications).where(communications.c.kind == "relance")).mappings().first()
        assert rel["status"] == "cancelled"
        assert later  # (variable utilisée uniquement pour la lisibilité)


def test_task_status_and_kpis(env):
    e, s = env
    with e.begin() as cx:
        importer.seed_events(cx, s)
        t = store.list_tasks(cx, store.get_event(cx, "BORDEAUX")["id"])[0]
        store.set_task_status(cx, t["id"], "done")
        k = store.kpis(cx, TODAY)
        assert k["tasks_done"] >= 1 and k["events_upcoming"] >= 8
        with pytest.raises(ValueError):
            store.set_task_status(cx, t["id"], "nimporte")


def test_import_staff(env):
    e, s = env
    with e.begin() as cx:
        store.upsert_event(cx, "PERIGUEUX", date=date(2026, 7, 9))
        csv_ = "Name,Prenom,Role,Shirt size,Hotel,Transport,Repas\nDUPONT,Marc,Arbitre,L,Ibis,Train,Oui\n,,,,,,\nDUPONT,Marc,Arbitre,L,,,\n"
        rep = importer.import_staff(cx, csv_, "periguex")
        assert rep.warnings                                  # nom d'événement mal orthographié : signalé
        rep = importer.import_staff(cx, csv_, "PERIGUEUX")
        assert (rep.created, rep.skipped) == (1, 2)
        assert store.list_staff(cx, store.get_event(cx, "PERIGUEUX")["id"])[0]["hotel"] == "Ibis"


# ----------------------------------------------------------------------------- fiabilité du moteur
def _two_events(e, s):
    with e.begin() as cx:
        ids = []
        for name in ("AAA", "BBB"):
            eid, _ = store.upsert_event(cx, name, date=date(2026, 10, 30), venue="Club")
            pid, _ = store.upsert_person(cx, prenom=name.title(), nom="Joueur", email=f"{name.lower()}@x.fr")
            store.upsert_registration(cx, eid, pid, type="Tournoi")
            ids.append(eid)
    return ids


def test_error_on_one_event_does_not_block_the_others_and_is_retried(env, monkeypatch):
    e, s = env
    ids = _two_events(e, s)
    real = eng.render

    def flaky(name, **ctx):
        if ctx["event"]["name"] == "AAA":
            raise RuntimeError("modèle cassé")
        return real(name, **ctx)

    monkeypatch.setattr(eng, "render", flaky)
    first = eng.run(e, s, TODAY)
    assert first.errors == 1 and first.invitations == 1          # BBB est passé malgré l'erreur sur AAA
    with e.connect() as cx:
        runs = store.list_runs(cx, status="erreur")
        assert len(runs) == 1 and runs[0]["event_name"] == "AAA" and "modèle cassé" in runs[0]["error"]
        assert cx.execute(select(communications)).mappings().all()[0]["to_addr"] == "bbb@x.fr"
    monkeypatch.setattr(eng, "render", real)                      # reprise : la panne est corrigée
    second = eng.run(e, s, TODAY)
    assert second.errors == 0 and second.invitations == 1 and second.skipped_existing == 1
    with e.connect() as cx:
        assert store.latest_run_status(cx, "invitations", ids[0]) == "ok"
        assert store.consecutive_failures(cx, "invitations", ids[0]) == 0


def test_three_consecutive_failures_raise_a_validation(env, monkeypatch):
    e, s = env
    _two_events(e, s)
    monkeypatch.setattr(eng, "render", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("panne")))
    for _ in range(3):
        eng.run(e, s, TODAY)
    with e.connect() as cx:
        from padel.db import pending
        rows = cx.execute(select(pending).where(pending.c.kind == "automatisation_en_echec")).mappings().all()
        assert len(rows) == 2 and all("échec répété" in r["title"] for r in rows)   # une par événement
        assert max(r["attempt"] for r in store.list_runs(cx)) == 3


def test_disabled_automation_does_nothing(env):
    e, s = env
    _setup_invites(e, s)
    with e.begin() as cx:
        store.set_automation_enabled(cx, "invitations", False)
    assert eng.run(e, s, TODAY).invitations == 0
    with e.begin() as cx:
        store.set_automation_enabled(cx, "invitations", True)
    assert eng.run(e, s, TODAY).invitations == 1


def test_event_kpis(env):
    e, s = env
    eid = _setup_invites(e, s)
    eng.run(e, s, TODAY)
    with e.begin() as cx:
        importer.seed_events(cx, s)
        ev = store.get_event(cx, eid)
        store.ensure_tasks(cx, eid, s.templates())
        k = store.event_kpis(cx, ev, TODAY)
        assert k["registrations"] == 3 and k["to_invite"] == 3 and k["emails_to_handle"] == 1 and k["flagged"] == 1
        assert k["tasks_total"] == 22 and k["tasks_late"] > 0 and 0 <= k["progress"] < 1
        assert len(k["next_actions"]) <= 5


def test_light_migration_adds_missing_column(tmp_path):
    from sqlalchemy import text
    from padel.db import ensure_columns
    url = f"sqlite:///{tmp_path / 'old.db'}"
    e = make_engine(url)
    with e.begin() as cx:
        cx.execute(text("CREATE TABLE events (id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL UNIQUE)"))
    init_db(e)
    with e.connect() as cx:
        cols = {r[1] for r in cx.execute(text("PRAGMA table_info(events)"))}
    assert {"report", "courts", "date"} <= cols
    assert ensure_columns(e) == []                                  # idempotent


def test_failed_workflow_leaves_no_partial_writes(env, monkeypatch):
    """Si le workflow échoue au milieu, ce qu'il a déjà écrit est annulé (rien de moitié préparé)."""
    e, s = env
    with e.begin() as cx:
        eid, _ = store.upsert_event(cx, "AAA", date=date(2026, 10, 30), venue="Club")
        for n in ("Ana", "Bob"):
            pid, _ = store.upsert_person(cx, prenom=n, nom="X", email=f"{n.lower()}@x.fr")
            store.upsert_registration(cx, eid, pid, type="Tournoi")
    real = eng.render
    calls = []

    def second_fails(name, **ctx):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boum")
        return real(name, **ctx)

    monkeypatch.setattr(eng, "render", second_fails)
    res = eng.run(e, s, TODAY)
    assert res.errors == 1
    with e.connect() as cx:
        assert cx.execute(select(communications)).first() is None
