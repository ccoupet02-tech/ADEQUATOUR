"""Test de fumée de l'interface Streamlit (sans réseau : la lecture Notion est simulée)."""
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from padel import events as events_mod  # noqa: E402
from padel.events import Event  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_streamlit_cache():
    import streamlit as st
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def _fake_events(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    for k in ("NOTION_TOKEN", "NOTION_EVENTS_DB", "NOTION_TASKS_DB", "NOTION_PARTICIPANTS_DB"):
        monkeypatch.setenv(k, "x" * 32)
    monkeypatch.setattr(events_mod, "list_events",
                        lambda *a, **k: [Event(id="1", name="DIJON", date=date(2027, 1, 20), venue="Club")])


def test_password_gate_and_main_page(monkeypatch):
    _fake_events(monkeypatch)
    at = AppTest.from_file(str(Path(__file__).parent.parent / "streamlit_app.py"), default_timeout=30).run()
    assert not at.exception
    assert len(at.tabs) == 0  # verrouillé tant que le mot de passe n'est pas saisi
    at.text_input[0].set_value("mauvais").run()
    assert at.error
    at.text_input[0].set_value("pw").run()
    assert not at.exception, at.exception
    assert len(at.tabs) == 6
    assert at.checkbox[0].value is False  # simulation par défaut


def test_app_never_passes_send():
    from pathlib import Path
    src = Path("streamlit_app.py").read_text(encoding="utf-8")
    assert '"--send"' not in src


def test_missing_notion_secret_shows_message(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "pw")
    for k in ("NOTION_TOKEN", "NOTION_EVENTS_DB", "NOTION_TASKS_DB", "NOTION_PARTICIPANTS_DB"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("padel.config.load_dotenv", lambda *a, **k: None, raising=False)
    at = AppTest.from_file(str(Path(__file__).parent.parent / "streamlit_app.py"), default_timeout=30).run()
    at.text_input[0].set_value("pw").run()
    assert not at.exception, at.exception
    assert any("Connexion à Notion impossible" in e.value for e in at.error)
    assert any("NOTION_TOKEN" in w.value for w in at.warning)
