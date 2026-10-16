"""Parcours complet dans l'appli : formulaire Typeform (structure d'Angers) -> bouton « Synchroniser maintenant » -> tableau."""
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from padel import store
from padel.config import Settings
from padel.db import make_engine
from tests.test_signups import FakeTF  # noqa: F401

APP = str(__import__("pathlib").Path(__file__).parent.parent / "streamlit_app.py")
T = lambda i, v: {"field": {"id": i}, "type": "text", "text": v}  # noqa: E731
F = lambda i, t, ty: {"id": i, "type": ty, "title": t}  # noqa: E731


class RealForm:
    form = {"fields": [
        {"id": "g1", "type": "contact_info", "title": "Remplir vos coordonnées ", "properties": {"fields": [
            F("a", "First name", "short_text"), F("b", "Last name", "short_text"), F("c", "Phone number", "phone_number"),
            F("d", "Email", "email"), F("e", "Company", "short_text")]}},
        F("f", "Quelle fonction occupez-vous dans votre entreprise ? ", "short_text"),
        F("g", "Merci de préciser votre participation à l’évènement :", "multiple_choice"),
        F("h", "Souhaitez-vous participer seul ou avec un collaborateur ?", "multiple_choice"),
        {"id": "g2", "type": "contact_info", "title": "Veuillez renseigner les coordonnées de votre collaborateur",
         "properties": {"fields": [F("i", "Prénom", "short_text"), F("j", "Nom", "short_text")]}},
        F("k", "Quel est votre niveau de padel ?", "opinion_scale"),
        F("l", "Quels sont selon vous les 3 enjeux les plus prioritaires pour votre entreprise pour les 12 prochains mois ?", "multiple_choice")]}
    responses = [
        {"submitted_at": "2026-10-10T08:00:00Z", "answers": [T("a", "CAM"), T("b", "CAM"), {"field": {"id": "d"}, "type": "email", "email": "c@x.fr"},
                                                          T("e", "Moi"), T("f", "Test"), {"field": {"id": "k"}, "type": "number", "number": 5}]},
        {"submitted_at": "2026-10-11T08:00:00Z", "answers": [T("a", "Ana"), T("b", "Alpha"), {"field": {"id": "d"}, "type": "email", "email": "a@a.fr"},
                                                          T("e", "Société A"), T("f", "DG"), {"field": {"id": "k"}, "type": "number", "number": 4}]},
        {"submitted_at": "2026-10-12T08:00:00Z", "answers": [T("a", "Bob"), T("b", "Beta"), {"field": {"id": "d"}, "type": "email", "email": "b@b.fr"},
                                                          T("e", "Société B"), T("f", "RH"), T("i", "Eve"), T("j", "Gamma"),
                                                          {"field": {"id": "k"}, "type": "number", "number": 6}]}]

    def get_form(self, _):
        return self.form

    def iter_responses(self, _):
        yield from self.responses


def test_sync_button_fills_the_table(monkeypatch, tmp_path):
    st.cache_resource.clear()
    st.cache_data.clear()
    monkeypatch.setenv("APP_PASSWORD", "pw")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("TYPEFORM_TOKEN", "tfp_test")
    monkeypatch.setenv("PADEL_DEBUG", "1")
    monkeypatch.setattr(Settings, "typeform_client", lambda self: RealForm())
    at = AppTest.from_file(APP, default_timeout=60).run()
    at.text_input[0].set_value("pw").run()
    next(b for b in at.button if "14 événements" in b.label).click().run() if False else None
    at.sidebar.radio[0].set_value("page:Données").run()
    next(b for b in at.button if "14 événements" in b.label).click().run()
    with make_engine().begin() as cx:
        eid = store.get_event(cx, "BORDEAUX")["id"]
        store.update_event(cx, eid, typeform_id="FORM1")
    at.sidebar.radio[0].set_value(f"event:{eid}").run()
    assert not at.exception, at.exception
    next(b for b in at.button if b.label == "Synchroniser maintenant").click().run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    with make_engine().connect() as cx:
        regs = store.list_registrations(cx, eid)
    assert len(regs) == 4, [(r["prenom"], r["nom"]) for r in regs]
    m = {x.label: x.value for x in at.metric}
    assert m["Participants"] == "4" and m["Équipes de 2"] == "1" and m["Inscrits seuls"] == "2"
    st.cache_resource.clear()
