"""Test de fumée du dashboard Streamlit (base SQLite temporaire, aucun réseau)."""
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(Path(__file__).parent.parent / "streamlit_app.py")
PAGES = ["Accueil", "Événements", "Participants", "Automatisations", "À valider", "Historique des actions",
         "Erreurs et synchronisations", "Données", "Paramètres et connexions"]


@pytest.fixture()
def app(monkeypatch, tmp_path):
    st.cache_resource.clear()
    st.cache_data.clear()
    monkeypatch.setenv("APP_PASSWORD", "pw")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("PADEL_DEBUG", "1")
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.text_input[0].set_value("pw").run()
    assert not at.exception, at.exception
    yield at
    st.cache_resource.clear()


def goto(at, page):
    """page : nom d'une page, ou « event:<id> » pour la fiche d'un événement."""
    at.sidebar.radio[0].set_value(page if page.startswith("event:") else f"page:{page}").run()
    assert not at.exception, f"{page}: {at.exception}"
    return at


def test_password_gate(monkeypatch, tmp_path):
    st.cache_resource.clear()
    monkeypatch.setenv("APP_PASSWORD", "pw")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert len(at.metric) == 0
    at.text_input[0].set_value("faux").run()
    assert at.error
    assert len(at.metric) == 0


def test_missing_password_secret_is_explicit(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert any("APP_PASSWORD" in e.value for e in at.error)


def test_every_page_renders_on_empty_base(app):
    for page in PAGES:
        goto(app, page)


def test_seed_then_pages_show_data(app):
    goto(app, "Données")
    seed = next(b for b in app.button if "14 événements" in b.label)
    seed.click().run()
    assert not app.exception, app.exception
    assert any("14 créé" in s.value for s in app.success)               # message conservé après le rechargement
    goto(app, "Accueil")
    kpi = {m.label: m.value for m in app.metric}
    assert int(kpi["Événements à venir"]) >= 8
    for page in PAGES:
        goto(app, page)
    # le menu contient les événements de la base, construits dynamiquement
    labels = list(app.sidebar.radio[0].options)
    assert sum("↳" in o for o in labels) == 14 and any("BORDEAUX" in o for o in labels)
    from padel import store
    from padel.db import make_engine
    with make_engine().connect() as cx:
        bordeaux = store.get_event(cx, "BORDEAUX")["id"]
    goto(app, f"event:{bordeaux}")
    assert not app.exception, app.exception
    assert len(app.tabs) == 8
    assert [t.label for t in app.tabs] == ["Vue d'ensemble", "Participants et inscriptions", "Staff et intervenants",
                                           "Logistique", "Communications", "Automatisations", "Historique",
                                           "Bilan et suivi post-événement"]


def test_data_survives_a_new_session(app):
    """Régression : les écritures doivent être validées (commit) avant st.rerun()."""
    goto(app, "Données")
    next(b for b in app.button if "14 événements" in b.label).click().run()
    st.cache_resource.clear()
    from padel import store
    from padel.db import make_engine
    with make_engine().connect() as cx:
        assert len(store.list_events(cx)) == 14


def test_app_never_sends_mail():
    src = Path(APP).read_text(encoding="utf-8")
    assert "SmtpBackend" not in src and "GraphBackend" not in src and "smtplib" not in src


def test_create_event_form_persists_across_rerun(app):
    """Le formulaire appelle st.rerun() : l'événement et ses 22 tâches doivent être validés avant."""
    goto(app, "Événements")
    form = next(t for t in app.text_input if t.label == "Ville")
    form.set_value("Marseille")
    btn = next(b for b in app.button if "22 tâches" in b.label)
    btn.click().run()
    assert not app.exception, app.exception
    st.cache_resource.clear()
    from padel import store
    from padel.db import make_engine
    with make_engine().connect() as cx:
        ev = store.get_event(cx, "MARSEILLE")
        assert ev is not None and len(store.list_tasks(cx, ev["id"])) == 22


def test_every_event_page_renders_and_settings_are_honest(app):
    goto(app, "Données")
    next(b for b in app.button if "14 événements" in b.label).click().run()
    from padel import store
    from padel.db import make_engine
    with make_engine().connect() as cx:
        ids = [e["id"] for e in store.list_events(cx)]
    for eid in ids:
        goto(app, f"event:{eid}")
    goto(app, "Paramètres et connexions")
    text = " ".join(m.value for m in app.markdown)
    assert "Outlook" in text and "Teams" in text
    outlook = next(m.value for m in app.markdown if "Outlook" in m.value)
    assert "OK (vérifié)" not in outlook                       # jamais « OK » pour un connecteur inexistant
    assert any("non développé" in c.value for c in app.caption)


def test_angers_event_page_with_signups_renders(app):
    """Fiche ANGERS avec inscrits (équipes + solos) : participants et documents s'affichent sans erreur."""
    from pathlib import Path
    from padel import importer, store
    from padel.config import load_settings
    from padel.db import make_engine
    goto(app, "Données")
    next(b for b in app.button if "14 événements" in b.label).click().run()
    eng = make_engine()
    with eng.begin() as cx:
        importer.import_signups(cx, Path("examples/inscriptions-angers-exemple.csv").read_bytes(), "ANGERS", load_settings())
        eid = store.get_event(cx, "ANGERS")["id"]
    goto(app, f"event:{eid}")
    text = " ".join(c.value for c in app.caption)
    assert "6 équipe(s) de 2" in text and "3 seul(s)" in text
    next(b for b in app.button if b.label == "Calculer poules et planning").click().run()
    assert not app.exception, app.exception
    assert any("8 équipes · 2 poule(s)" in s.value for s in app.success)
    next(b for b in app.button if "Enregistrer ces poules" in b.label).click().run()
    with eng.connect() as cx:
        assert {r["poule"] for r in store.list_registrations(cx, eid)} == {"A", "B"}
