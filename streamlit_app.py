"""Interface web du pipeline Adéquat Padel Tour (Streamlit).

Elle ne fait qu'appeler la ligne de commande (`padel.cli.main`) : les garde-fous sont donc identiques.
  * rien n'est écrit dans Notion sans cocher « Écrire dans Notion » ;
  * AUCUN mail n'est jamais envoyé d'ici : seulement des aperçus (.eml) à télécharger.
Secrets (menu « Secrets » de Streamlit Cloud, jamais dans le code) : APP_PASSWORD, NOTION_TOKEN,
NOTION_EVENTS_DB, NOTION_TASKS_DB, NOTION_PARTICIPANTS_DB, et facultatif TYPEFORM_TOKEN.
"""
from __future__ import annotations

import contextlib
import hmac
import io
import os
import tempfile
from datetime import date
from pathlib import Path

import streamlit as st

# Les secrets Streamlit deviennent des variables d'environnement lues par load_settings().
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:  # pas de secrets.toml en local : on s'appuie sur .env
    pass

from padel import cli  # noqa: E402
from padel.config import load_settings  # noqa: E402
from padel.events import list_events  # noqa: E402
from padel.util import slug  # noqa: E402

st.set_page_config(page_title="Adéquat Padel Tour", page_icon="🎾", layout="wide")


# ----------------------------------------------------------------------------- accès
def check_password() -> bool:
    expected = os.environ.get("APP_PASSWORD", "")
    if not expected:
        st.error("Aucun mot de passe configuré : ajoutez APP_PASSWORD dans les Secrets de Streamlit.")
        return False
    if st.session_state.get("auth"):
        return True
    pwd = st.text_input("Mot de passe", type="password")
    if pwd:
        if hmac.compare_digest(pwd.encode(), expected.encode()):
            st.session_state["auth"] = True
            st.rerun()
        st.error("Mot de passe incorrect.")
    return False


# ----------------------------------------------------------------------------- appels CLI
def run(args: list[str], apply: bool) -> tuple[int, str]:
    """Exécute une commande du pipeline et renvoie (code, sortie). --send n'est JAMAIS transmis."""
    argv = list(args) + (["--apply"] if apply else [])
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0) if isinstance(exc.code, int) else 1
    return code, (buf_out.getvalue() + buf_err.getvalue()).strip()


def show(result: tuple[int, str]) -> None:
    code, text = result
    (st.success if code == 0 else st.error)("Terminé." if code == 0 else f"Échec (code {code}).")
    if text:
        st.code(text, language=None)


@st.cache_data(ttl=60, show_spinner="Lecture de Notion…")
def events_cache() -> list[dict]:
    settings = load_settings(None)
    notion = settings.notion_client(dry_run=True)
    return [{"name": e.name, "date": e.date, "venue": e.venue} for e in list_events(notion, settings)]


def out_dir(event: str) -> Path:
    return load_settings(None).out_dir / slug(event)


def downloads(folder: Path, patterns: tuple[str, ...]) -> None:
    files = sorted({p for pat in patterns for p in folder.glob(pat)})
    if not files:
        return
    cols = st.columns(min(len(files), 4))
    for i, f in enumerate(files):
        cols[i % len(cols)].download_button(f"⬇ {f.name}", f.read_bytes(), file_name=f.name, key=f"dl-{folder}-{f.name}")


# ----------------------------------------------------------------------------- page
st.title("🎾 Adéquat Padel Tour")
if not check_password():
    st.stop()

with st.sidebar:
    st.header("Mode")
    apply = st.checkbox("✍️ Écrire dans Notion", value=False,
                        help="Décoché = simulation : rien n'est modifié dans Notion.")
    st.info("ÉCRITURE ACTIVE" if apply else "Simulation : aucune écriture.")
    st.caption("Les mails ne sont jamais envoyés depuis cette interface (aperçus uniquement).")
    if st.button("Recharger les événements"):
        events_cache.clear()

try:
    events = events_cache()
except Exception as exc:  # noqa: BLE001
    st.error(f"Connexion à Notion impossible : {exc}")
    st.caption("Vérifiez NOTION_TOKEN et les 3 identifiants de base dans les Secrets, et que l'intégration "
               "est ajoutée à chaque base (menu « … » → Connexions).")
    st.stop()

names = [e["name"] for e in events]
tab_ev, tab_new, tab_part, tab_doc, tab_mail, tab_ctl = st.tabs(
    ["📅 Événements", "➕ Nouvel événement", "👥 Participants", "📄 Documents", "✉️ Mails (aperçu)", "🔎 Contrôles"])

with tab_ev:
    rows = [{"Événement": e["name"], "Date": e["date"], "Lieu": e["venue"],
             "Jours restants": (e["date"] - date.today()).days if e["date"] else None} for e in events]
    st.dataframe(rows, use_container_width=True, hide_index=True)
    c1, c2, c3 = st.columns(3)
    if c1.button("Retards et échéances"):
        show(run(["digest"], False))
    if c2.button("Compléter les tâches manquantes (tous)"):
        show(run(["sync-tasks", "--all"], apply))
    if c3.button("Mettre à jour les synthèses"):
        show(run(["refresh"], apply))

with tab_new:
    with st.form("new_event"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Ville (nom de l'événement)", placeholder="LILLE")
        day = c2.date_input("Date", value=date.today())
        venue = c1.text_input("Lieu / salle")
        dr = c2.text_input("DR")
        ok = st.form_submit_button("Créer")
    if ok:
        if not name.strip():
            st.warning("Indiquez le nom de l'événement.")
        else:
            args = ["create-event", name.strip().upper(), day.isoformat()]
            if venue:
                args += ["--venue", venue]
            if dr:
                args += ["--dr", dr]
            show(run(args, apply))
            events_cache.clear()

with tab_part:
    ev = st.selectbox("Événement", names, key="ev_part")
    c1, c2 = st.columns(2)
    if c1.button("Importer depuis Typeform"):
        show(run(["sync-participants", "--event", ev], apply))
    if c2.button("Voir les questions Typeform reconnues"):
        show(run(["typeform-fields", "--event", ev], False))
    up = st.file_uploader("…ou importer un CSV de participants", type="csv")
    if up and st.button("Importer le CSV"):
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
            tmp.write(up.getvalue())
        show(run(["import-csv", "--event", ev, tmp.name], apply))

with tab_doc:
    ev = st.selectbox("Événement", names, key="ev_doc")
    c1, c2, c3 = st.columns(3)
    courts = c1.number_input("Terrains", 1, 12, 4)
    minutes = c2.number_input("Minutes par match", 5, 60, 20)
    write_poules = c3.checkbox("Écrire les poules dans Notion (si écriture active)")
    if st.button("Générer poules, planning et PDF"):
        args = ["documents", "--event", ev, "--courts", str(courts), "--minutes", str(minutes)]
        if write_poules:
            args.append("--write-poules")
        show(run(args, apply))
    downloads(out_dir(ev), ("*.pdf",))
    if st.button("Calculer t-shirts, repas et staff"):
        show(run(["logistics", "--event", ev], False))
    downloads(out_dir(ev), ("*.csv", "*.md"))

with tab_mail:
    ev = st.selectbox("Événement", names, key="ev_mail")
    kind = st.selectbox("Type", ["invitation", "relance", "recap-kam", "retour"])
    st.caption("Génère des fichiers .eml à relire. L'envoi réel se fait volontairement hors de cette interface.")
    if st.button("Préparer l'aperçu"):
        show(run(["mail", kind, "--event", ev], False))
    downloads(out_dir(ev) / "mails", ("*.eml",))

with tab_ctl:
    c1, c2 = st.columns(2)
    if c1.button("Audit de la to-do"):
        show(run(["audit"], False))
    if c2.button("Liste des événements (texte)"):
        show(run(["list-events"], False))
