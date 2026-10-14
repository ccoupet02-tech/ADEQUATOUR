"""Les connexions ne doivent jamais être affichées « OK » sans vérification réelle."""
from padel import connections
from padel.db import init_db, make_engine


def test_outlook_and_teams_never_ok_even_with_secrets(monkeypatch):
    for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"):
        monkeypatch.setenv(k, "x")
    for c in connections.check_microsoft():
        assert c.state == "configure" and not c.developed          # jamais « ok » : le connecteur n'existe pas
    for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"):
        monkeypatch.delenv(k)
    assert all(c.state == "absent" for c in connections.check_microsoft())


def test_typeform_states(monkeypatch):
    monkeypatch.delenv("TYPEFORM_TOKEN", raising=False)
    assert connections.check_typeform().state == "absent"
    monkeypatch.setenv("TYPEFORM_TOKEN", "tfp_x")
    assert connections.check_typeform(test=False).state == "configure"   # présent mais non testé : pas « ok »

    class Resp:
        status_code = 401

    monkeypatch.setattr(connections.requests, "get", lambda *a, **k: Resp())
    assert connections.check_typeform(test=True).state == "erreur"
    Resp.status_code = 200
    assert connections.check_typeform(test=True).state == "ok"


def test_claude_key_present_is_not_ok(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert connections.check_claude().state == "configure"


def test_database_and_engine_status(tmp_path):
    from datetime import datetime, timedelta
    from padel import store
    e = make_engine(f"sqlite:///{tmp_path / 'c.db'}")
    init_db(e)
    assert connections.check_database(e).state == "configure"            # SQLite local = temporaire
    with e.begin() as cx:
        assert connections.check_engine(cx).state == "absent"            # aucun passage enregistré
        store.log(cx, "engine_run", None, "x")
    with e.begin() as cx:
        assert connections.check_engine(cx).state == "ok"
        assert connections.check_engine(cx, today=datetime.utcnow() + timedelta(hours=3)).state == "erreur"
