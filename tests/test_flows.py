"""Flux complets contre le faux Notion : événement, tâches, participants, mails, synthèse, audit."""
from datetime import date

import pytest

from conftest import FakeTypeform
from padel import campaigns
from padel.audit import audit_tasks
from padel.events import SUMMARY_MARKER, create_event, list_events
from padel.mailer import DryRunBackend, render
from padel.notify import build_digest
from padel.notion import block_text
from padel.participants import fetch_participants, sync_participants, upsert_people
from padel.summary import refresh_summary
from padel.tasks import ensure_event_tasks, fetch_tasks, mark_task


def test_creation_evenement_complete(notion, settings):
    ev, created = create_event(notion, settings, "RENNES", date(2026, 10, 8), venue="Salle X", dr="Patrice")
    assert len(created) == len(settings.templates())
    assert notion.find_child_database(ev.id, "Staff")                   # base Staff posée sur la page
    texts = [block_text(b) for b in notion.list_children(ev.id)]
    assert any("Contact sur place" in t for t in texts) and any(SUMMARY_MARKER in t for t in texts)
    tasks = fetch_tasks(notion, settings, ev.id)
    devis = next(t for t in tasks if t.key == "demande devis")
    assert devis.offset == 60 and devis.category == "Lieu" and devis.status == "Not started"
    assert devis.deadline(ev.date) == date(2026, 8, 9)                   # cf. to-do d'origine (RENNES)
    with pytest.raises(ValueError):                                     # pas de doublon d'événement
        create_event(notion, settings, "rennes", date(2026, 10, 9))


def test_tasks_idempotent_et_reconnait_les_suffixes(notion, settings, event):
    schema = settings.schema("tasks")
    notion.create_page({"database_id": "db-tasks"},
                       schema.build({"name": "Invitation (1)", "offset": 27, "status": "In progress",
                                     "event": [event.id]}))
    first = ensure_event_tasks(notion, settings, event)
    assert "Invitation" not in first and len(first) == len(settings.templates()) - 1
    assert ensure_event_tasks(notion, settings, event) == []             # 2e passage : rien à créer


def test_mark_task_ne_retrograde_jamais_un_done(notion, settings, event):
    ensure_event_tasks(notion, settings, event)
    assert mark_task(notion, settings, event, "relance", "Done") is True
    assert mark_task(notion, settings, event, "relance", "In progress") is False
    rel = next(t for t in fetch_tasks(notion, settings, event.id) if t.key == "relance invitation")
    assert rel.status == "Done"


FORM = {"fields": [
    {"id": "f1", "ref": "r1", "title": "Votre prénom", "type": "short_text"},
    {"id": "f2", "ref": "r2", "title": "Votre nom", "type": "short_text"},
    {"id": "f3", "ref": "r3", "title": "Votre e-mail", "type": "email"},
    {"id": "f4", "ref": "r4", "title": "Taille de t-shirt", "type": "multiple_choice"},
    {"id": "f5", "ref": "r5", "title": "Nom et prénom de votre coéquipier (laisser vide si seul)", "type": "short_text"},
    {"id": "f6", "ref": "r6", "title": "E-mail de votre coéquipier", "type": "email"},
    {"id": "f7", "ref": "r7", "title": "Quel est votre niveau ?", "type": "multiple_choice"}]}


def _resp(rid, when, **a):
    kinds = {"f1": "text", "f2": "text", "f3": "email", "f5": "text", "f6": "email"}
    answers = []
    for fid, val in a.items():
        if fid in ("f4", "f7"):
            answers.append({"field": {"id": fid}, "type": "choice", "choice": {"label": val}})
        else:
            answers.append({"field": {"id": fid}, "type": kinds[fid], kinds[fid]: val})
    return {"response_id": rid, "submitted_at": when, "answers": answers}


def test_sync_typeform_cree_le_coequipier_et_reste_idempotent(notion, settings, event):
    tf = FakeTypeform(FORM, [
        _resp("a", "2026-09-01T10:00:00Z", f1="Alice", f2="Martin", f3="Alice@Ex.fr", f4="M",
              f5="Bruno Durand", f6="bruno@ex.fr", f7="3 - Confirmé"),
        _resp("b", "2026-09-02T10:00:00Z", f1="Chloé", f2="Petit", f3="chloe@ex.fr", f4="XS")])
    settings.raw["typeform"]["forms"] = {"DIJON": "form1"}
    rep = sync_participants(notion, settings, event, tf)
    assert (rep.created, rep.updated) == (3, 0)
    people = {p.email: p for p in fetch_participants(notion, settings, event.id)}
    assert people["alice@ex.fr"].size == "M" and people["alice@ex.fr"].team == "Durand / Martin"
    assert people["bruno@ex.fr"].team == "Durand / Martin" and people["bruno@ex.fr"].prenom == "Bruno"
    assert people["chloe@ex.fr"].size == ""          # XS absent de Notion -> laissé vide plutôt que faux
    again = sync_participants(notion, settings, event, tf)
    assert (again.created, again.updated, again.unchanged) == (0, 0, 3)


def test_upsert_complete_sans_ecraser(notion, settings, event):
    upsert_people(notion, settings, event, [{"prenom": "Alice", "nom": "Martin", "email": "a@x.fr", "size": "M"}])
    rep = upsert_people(notion, settings, event, [{"prenom": "Alice", "nom": "Martin", "email": "a@x.fr",
                                                   "size": "L", "company": "ACME"}])
    p = fetch_participants(notion, settings, event.id)[0]
    assert rep.updated == 1 and p.size == "M" and p.company == "ACME"    # taille existante conservée
    upsert_people(notion, settings, event, [{"prenom": "Alice", "nom": "Martin", "email": "a@x.fr", "size": "L"}],
                  overwrite=True)
    assert fetch_participants(notion, settings, event.id)[0].size == "L"


def test_invitation_dry_run_n_ecrit_rien(notion, settings, event, tmp_path):
    upsert_people(notion, settings, event, [{"prenom": "Alice", "nom": "Martin", "email": "a@x.fr"},
                                            {"prenom": "Sans", "nom": "Mail"}])
    backend = DryRunBackend(tmp_path)
    res = campaigns.invitation(notion, settings, event, fetch_participants(notion, settings, event.id), backend)
    assert res.sent == 1 and not res.real and len(res.skipped) == 1
    assert len(list(tmp_path.glob("*.eml"))) == 1
    assert all(p.invite_status == "" for p in fetch_participants(notion, settings, event.id))


def test_invitation_reelle_met_a_jour_statut_et_tache(notion, settings, event):
    ensure_event_tasks(notion, settings, event)
    upsert_people(notion, settings, event, [{"prenom": "Alice", "nom": "Martin", "email": "a@x.fr"}])

    class Real:
        real = True
        sent = []

        def send(self, mail):
            self.sent.append(mail)

    backend = Real()
    res = campaigns.invitation(notion, settings, event, fetch_participants(notion, settings, event.id), backend)
    assert res.sent == 1 and backend.sent[0].to == ["a@x.fr"]
    assert fetch_participants(notion, settings, event.id)[0].invite_status == "Pas de réponse"
    inv = next(t for t in fetch_tasks(notion, settings, event.id) if t.key == "invitation")
    assert inv.status == "Done"
    # 2e passage : déjà invité -> aucun renvoi
    assert campaigns.invitation(notion, settings, event, fetch_participants(notion, settings, event.id),
                                Real()).sent == 0


def test_modeles_de_mail_exigent_toutes_les_variables(settings):
    ctx = {"brand": "APT", "signature": "Équipe", "p": type("P", (), {"prenom": "Alice"})(),
           "event": {"name": "DIJON", "date_fr": "01/10/2026", "venue": "", "register_url": "",
                     "photos_url": "", "feedback_url": ""}}
    subject, body = render("invitation", **ctx)
    assert "DIJON" in subject and "Bonjour Alice" in body
    ctx.pop("signature")
    with pytest.raises(Exception):
        render("invitation", **ctx)


def test_synthese_mise_a_jour_en_place(notion, settings, event):
    ensure_event_tasks(notion, settings, event)
    notion.append_children(event.id, [{"object": "block", "type": "callout",
                                       "callout": {"rich_text": [{"type": "text", "text": {"content": SUMMARY_MARKER + " vide"}}]}}])
    n_blocks = len(notion.list_children(event.id))
    text = refresh_summary(notion, settings, event, fetch_tasks(notion, settings, event.id),
                           fetch_participants(notion, settings, event.id), date(2026, 9, 20))
    assert "en retard" in text and "DIJON" in text
    assert len(notion.list_children(event.id)) == n_blocks               # bloc existant mis à jour, pas dupliqué
    assert "Tâches : 0/22" in block_text(notion.list_children(event.id)[0])


def test_digest_et_audit(notion, settings, event):
    ensure_event_tasks(notion, settings, event)
    digest = build_digest(settings, date(2026, 9, 20), [(event, fetch_tasks(notion, settings, event.id))])
    assert "DIJON" in digest and "en retard" in digest and "Demande devis" in digest
    calm = build_digest(settings, date(2026, 1, 1), [(event, fetch_tasks(notion, settings, event.id))])
    assert "Rien d'urgent" in calm
    schema = settings.schema("tasks")
    notion.create_page({"database_id": "db-tasks"},
                       schema.build({"name": "Demande devis (1)", "offset": 40, "event": [event.id]}))
    notion.create_page({"database_id": "db-tasks"}, schema.build({"name": "Tâche inconnue", "event": [event.id]}))
    issues = " | ".join(audit_tasks(settings, fetch_tasks(notion, settings), list_events(notion, settings)))
    assert "Offset différent" in issues and "hors référentiel" in issues and "suffixe" in issues
    assert "Doublon" in issues
