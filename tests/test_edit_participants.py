"""Modification / ajout / retrait manuels dans les tableaux, et protection face à la synchro Typeform."""
from padel import importer, store
from tests.test_signups import RESPONSES, FakeTF, env  # noqa: F401


def test_edit_add_remove_and_locks_survive_sync(env):
    eng, s = env
    with eng.begin() as cx:
        eid = store.get_event(cx, "ANGERS")["id"]
        store.update_event(cx, eid, typeform_id="FORM1")
        importer.sync_typeform(cx, "ANGERS", FakeTF(RESPONSES), s)
        regs = store.list_registrations(cx, eid)
        alice = next(r for r in regs if r["nom"] == "Roy")
        chloe = next(r for r in regs if r["nom"] == "Vidal")
        store.update_participant(cx, alice["id"], {"company": "Nouvelle Co"}, {"level": "2", "invite_status": "accepte"})
        rid = store.add_participant(cx, eid, prenom="Zoé", nom="Zed", company="Z", level="3")
        assert store.remove_registration(cx, chloe["id"]) == "ignoree"
        assert store.remove_registration(cx, rid) == "supprimee"
        store.set_team(cx, [alice["id"]], "Equipe Perso")
    with eng.begin() as cx:
        importer.sync_typeform(cx, "ANGERS", FakeTF(RESPONSES), s)  # la synchro ne doit rien défaire
        regs = store.list_registrations(cx, eid)
    a = next(r for r in regs if r["nom"] == "Roy")
    assert a["level"] == "2" and a["company"] == "Nouvelle Co" and a["team"] == "Equipe Perso"
    assert not any(r["nom"] == "Vidal" for r in regs)
    assert not any(r["nom"] == "Zed" for r in regs)
