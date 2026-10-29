"""Dashboard Adéquat Padel Tour : l'application est la source de vérité (base Neon/PostgreSQL).

Secrets (Streamlit Cloud > Settings > Secrets, jamais dans le code) : APP_PASSWORD, DATABASE_URL.
Phase 1 : le moteur prépare des aperçus de communications et signale ce qui demande une décision ; aucun envoi
automatique n'est branché (Outlook/Teams viendront ensuite, après validation des autorisations).
Aucune connexion n'est affichée « OK » sans avoir été vérifiée par un appel réel (voir padel/connections.py).
"""
from __future__ import annotations

import hmac
import io
import os
import re
import zipfile
from datetime import datetime, date

import pandas as pd
import streamlit as st

try:  # les secrets Streamlit deviennent des variables d'environnement
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:  # pas de secrets.toml en local
    pass

from sqlalchemy import select  # noqa: E402

from padel import connections, engine as automation, importer, store  # noqa: E402
from padel.config import load_settings  # noqa: E402
from padel.db import (action_log, communications, describe, events as t_events, init_db, make_engine,  # noqa: E402
                      pending, people, registrations, staff as t_staff, tasks as t_tasks)
from padel.util import fr_date, slug  # noqa: E402

st.set_page_config(page_title="Adéquat Padel Tour", page_icon="🎾", layout="wide")
TODAY = date.today()
TASK_LABELS = store.TASK_STATUS
INVITE_LABELS = store.INVITE_STATUS
EVENT_STATUS = {"preparation": "En préparation", "en_cours": "En cours", "termine": "Terminé", "annule": "Annulé"}
KIND_LABELS = {"invitation": "Invitation", "relance": "Relance", "confirmation": "Confirmation"}


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


@st.cache_resource(show_spinner="Connexion à la base…")
def _engine_for(schema: str):  # `schema` change => la migration est rejouée même si le processus n'a pas redémarré
    engine = make_engine()
    init_db(engine)
    return engine


def get_engine():
    from padel.db import schema_signature
    return _engine_for(schema_signature())


@st.cache_resource
def _settings_for(version: str):  # `version` change => config.yaml rechargé sans redémarrer le processus
    return load_settings(use_dotenv=False)


def get_settings():
    from pathlib import Path
    cfg = Path(__file__).parent / "config.yaml"
    stat = cfg.stat()
    return _settings_for(f"{stat.st_mtime_ns}-{stat.st_size}-{bool(os.environ.get('TYPEFORM_TOKEN'))}")


def fmt_dt(value) -> str:
    return value.strftime("%d/%m/%Y %H:%M") if value else ""


def progress_of(task_list: list[dict]) -> float:
    return (sum(t["status"] == "done" for t in task_list) / len(task_list)) if task_list else 0.0



def flash(message: str, level: str = "success") -> None:
    """Message affiché après le rechargement de la page (st.rerun efface ce qui a été écrit avant)."""
    st.session_state.setdefault("flash", []).append((level, message))


def rerun(cx):
    """Valide les écritures puis relance la page (st.rerun interrompt le script : sans commit, tout serait annulé)."""
    cx.commit()
    st.rerun()




def _since_midnight():
    from datetime import datetime
    return datetime.combine(TODAY, datetime.min.time())


def page_home(cx):
    soon = get_settings().get("digest", "soon_days", default=7)
    evs = store.list_events(cx)
    kp = {e["id"]: store.event_kpis(cx, e, TODAY, soon) for e in evs}
    upcoming = [e for e in evs if e["date"] and e["date"] >= TODAY and e["status"] not in ("termine", "annule")]
    open_pending = cx.execute(select(pending).where(pending.c.status == "open").order_by(pending.c.id)).mappings().all()
    previews = cx.execute(select(communications, t_events.c.name.label("event_name")).join(
        t_events, t_events.c.id == communications.c.event_id, isouter=True).where(
        communications.c.status.in_(("preview", "approved"))).order_by(communications.c.id.desc())).mappings().all()
    failing = [r for r in store.list_runs(cx, status="erreur", limit=300)
               if store.latest_run_status(cx, r["workflow"], r["event_id"]) == "erreur"]
    failing = list({(r["workflow"], r["event_id"]): r for r in failing}.values())
    today_actions = cx.execute(select(action_log).where(action_log.c.ts >= _since_midnight())
                               .order_by(action_log.c.id.desc())).mappings().all()
    auto_today = [a for a in today_actions if a["actor"] == "systeme"]
    last_run = cx.execute(select(action_log.c.ts).where(action_log.c.action == "engine_run")
                          .order_by(action_log.c.id.desc()).limit(1)).scalar()

    c = st.columns(6)
    c[0].metric("Événements à venir", len(upcoming))
    c[1].metric("À valider", len(open_pending))
    c[2].metric("E-mails à traiter", len(previews))
    c[3].metric("Automatisations en erreur", len(failing))
    c[4].metric("Actions automatiques aujourd'hui", len(auto_today))
    c[5].metric("Tâches en retard", sum(k["tasks_late"] for e in upcoming for k in [kp[e["id"]]]))
    st.caption(f"Dernier passage du moteur : {fmt_dt(last_run) or 'jamais'}")

    st.subheader("Quels événements arrivent bientôt ?")
    if upcoming:
        st.dataframe(pd.DataFrame([{
            "Événement": e["name"], "Date": e["date"], "J-": (e["date"] - TODAY).days, "Lieu": e["venue"],
            "Préparation": kp[e["id"]]["progress"], "Inscrits": kp[e["id"]]["registrations"],
            "Confirmés": kp[e["id"]]["accepted"]} for e in upcoming[:8]]), hide_index=True, column_config={
            "Préparation": st.column_config.ProgressColumn("Préparation", min_value=0.0, max_value=1.0, format="percent"),
            "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY")})
    else:
        st.info("Aucun événement à venir. Chargez le jeu de départ dans « Données » ou créez un événement.")

    st.subheader("Quels événements nécessitent mon attention ?")
    attention = []
    for e in upcoming:
        k = kp[e["id"]]
        reasons = []
        if k["tasks_late"]:
            reasons.append(f"{k['tasks_late']} tâche(s) en retard")
        if k["failing"]:
            reasons.append("automatisation en erreur : " + ", ".join(k["failing"]))
        if k["flagged"]:
            reasons.append(f"{k['flagged']} élément(s) à valider")
        if k["no_answer"]:
            reasons.append(f"{k['no_answer']} invitation(s) sans réponse")
        if reasons:
            attention.append({"Événement": e["name"], "J-": (e["date"] - TODAY).days, "Pourquoi": " · ".join(reasons)})
    if attention:
        st.dataframe(pd.DataFrame(attention), hide_index=True)
    else:
        st.success("Aucun événement ne demande d'attention particulière.")

    left, right = st.columns(2)
    with left:
        st.subheader("E-mails et relances en attente")
        if previews:
            st.dataframe(pd.DataFrame([{"Événement": r["event_name"], "Type": KIND_LABELS.get(r["kind"], r["kind"]),
                                        "Destinataire": r["to_addr"], "Statut": r["status"]} for r in previews[:10]]),
                         hide_index=True)
            if len(previews) > 10:
                st.caption(f"… et {len(previews) - 10} autres (page Automatisations).")
        else:
            st.caption("Aucun aperçu en attente.")
        st.subheader("Actions réalisées automatiquement aujourd'hui")
        if auto_today:
            st.dataframe(pd.DataFrame([{"Heure": a["ts"].strftime("%H:%M"), "Action": a["action"], "Détail": a["detail"]}
                                       for a in auto_today[:10]]), hide_index=True)
        else:
            st.caption("Aucune action automatique aujourd'hui.")
    with right:
        st.subheader("Automatisations en erreur")
        if failing:
            st.dataframe(pd.DataFrame([{"Événement": r["event_name"], "Workflow": r["workflow"],
                                        "Échecs": r["attempt"], "Erreur": (r["error"] or "").split("\n")[0][:120]}
                                       for r in failing]), hide_index=True)
        else:
            st.success("Aucune automatisation en erreur.")
        st.subheader("Décisions à prendre")
        if open_pending:
            st.dataframe(pd.DataFrame([{"Sujet": p["title"], "Pourquoi": p["reason"]} for p in open_pending[:10]]),
                         hide_index=True)
            st.caption("Traitez-les dans « À valider ».")
        else:
            st.success("Aucune décision en attente.")



def open_event(event_id: int):
    st.session_state["nav"] = f"event:{event_id}"


def page_events(cx):
    with st.expander("➕ Nouvel événement"):
        with st.form("new-event", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            name, day, venue = c1.text_input("Ville"), c2.date_input("Date", format="DD/MM/YYYY"), c3.text_input("Lieu")
            dr = st.text_input("Responsable (DR)")
            if st.form_submit_button("Créer avec les 22 tâches du référentiel") and name.strip():
                eid, created = store.upsert_event(cx, name, date=day, venue=venue, dr=dr)
                n = store.ensure_tasks(cx, eid, get_settings().templates())
                store.log(cx, "event_created" if created else "event_exists", eid, name.upper(), "utilisateur")
                flash(f"{name.upper()} : {'créé' if created else 'existait déjà'}, {n} tâche(s) ajoutée(s).")
                rerun(cx)
    evs = store.list_events(cx)
    if not evs:
        st.info("Aucun événement : ouvrez « Données » et chargez le jeu de départ, ou créez-en un ci-dessus.")
        return
    soon = get_settings().get("digest", "soon_days", default=7)
    st.caption("Cliquez sur « Ouvrir » pour accéder à la fiche opérationnelle d'un événement.")
    for e in evs:
        k = store.event_kpis(cx, e, TODAY, soon)
        c = st.columns([2, 1.2, 2, 1.4, 1.6, 1])
        c[0].markdown(f"**{e['name']}**  \n{e['venue'] or ''}")
        c[1].write(fr_date(e["date"]) + (f"  \nJ-{(e['date'] - TODAY).days}" if e["date"] and e["date"] >= TODAY else ""))
        c[2].progress(k["progress"], text=f"{k['tasks_done']}/{k['tasks_total']} tâches")
        c[3].write(f"{k['accepted']}/{k['registrations']} confirmés")
        c[4].write(("🔴 " + str(k["tasks_late"]) + " en retard" if k["tasks_late"] else "") +
                   ("  \n⚠️ erreur" if k["failing"] else ""))
        c[5].button("Ouvrir", key=f"open-{e['id']}", on_click=open_event, args=(e["id"],))


def section_overview(cx, ev):
    k = store.event_kpis(cx, ev, TODAY, get_settings().get("digest", "soon_days", default=7))
    c = st.columns(4)
    c[0].metric("Inscrits / confirmés", f"{k['registrations']} / {k['accepted']}")
    c[1].metric("Invitations sans réponse", k["no_answer"], help=f"{k['to_invite']} personne(s) pas encore invitée(s)")
    c[2].metric("E-mails à traiter", k["emails_to_handle"])
    c[3].metric("Éléments à valider", k["flagged"])
    c = st.columns(4)
    c[0].metric("Tâches en retard", k["tasks_late"])
    c[1].metric("À faire sous 7 jours", k["tasks_soon"])
    c[2].metric("Automatisations en erreur", len(k["failing"]))
    c[3].metric("Avancement", f"{k['progress']:.0%}")
    st.progress(k["progress"], text=f"{k['tasks_done']} / {k['tasks_total']} tâches terminées")
    if k["failing"]:
        st.error("Automatisation(s) en erreur : " + ", ".join(k["failing"]) + " (voir « Automatisations »).")
    st.markdown("**Actions à venir**")
    if k["next_actions"]:
        st.dataframe(pd.DataFrame([{"Échéance": t["deadline"], "J": (t["deadline"] - TODAY).days, "Tâche": t["name"],
                                    "Catégorie": t["category"]} for t in k["next_actions"]]), hide_index=True,
                     column_config={"Échéance": st.column_config.DateColumn("Échéance", format="DD/MM/YYYY")})
    else:
        st.caption("Aucune action à venir.")
    st.markdown("**Préparation (tâches)**")
    tab_tasks(cx, ev)
    with st.expander("Caractéristiques de l'événement (lieu, contact, prix, terrains)"):
        tab_details(cx, ev)


def section_automations(cx, ev):
    st.caption("Les workflows préparent des aperçus ; rien n'est envoyé. Désactivez-les dans « Paramètres et connexions ».")
    rows = []
    for a in store.list_automations(cx):
        rows.append({"Workflow": a["label"], "Actif": "oui" if a["enabled"] else "non",
                     "Dernier résultat sur cet événement": store.latest_run_status(cx, a["key"], ev["id"]) or "jamais exécuté"})
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    c1, c2 = st.columns(2)
    if c1.button("Simuler pour cet événement", key=f"sim-{ev['id']}"):
        st.info(automation.run(get_engine(), get_settings(), TODAY, dry_run=True, trigger="manuel", only_event=ev["id"]).summary())
    if c2.button("Lancer pour cet événement", key=f"run-{ev['id']}"):
        res = automation.run(get_engine(), get_settings(), TODAY, trigger="manuel", only_event=ev["id"])
        (st.error if res.errors else st.success)(res.summary())
        for n in res.notes:
            st.warning(n)
    runs = store.list_runs(cx, ev["id"], limit=50)
    if runs:
        st.markdown("**Exécutions**")
        st.dataframe(pd.DataFrame([{"Date": fmt_dt(r["started_at"]), "Workflow": r["workflow"], "Statut": r["status"],
                                    "Préparés": r["prepared"], "À valider": r["flagged"], "Essai n°": r["attempt"],
                                    "Erreur": (r["error"] or "").split("\n")[0][:100]} for r in runs]), hide_index=True)


def section_report(cx, ev):
    k = store.event_kpis(cx, ev, TODAY)
    sent = cx.execute(select(communications).where((communications.c.event_id == ev["id"]) &
                                                   (communications.c.status == "sent"))).mappings().all()
    answered = k["accepted"] + k["refused"]
    invited = k["registrations"] - k["to_invite"]
    rate = f"{answered / invited:.0%}" if invited else "n/a"
    c = st.columns(5)
    c[0].metric("Inscrits", k["registrations"])
    c[1].metric("Confirmés", k["accepted"])
    c[2].metric("Refus", k["refused"])
    c[3].metric("Taux de réponse", rate)
    c[4].metric("Communications envoyées", len(sent))
    st.caption("Les présents effectifs et les retours de questionnaire ne sont pas encore collectés : ils ne sont pas inventés ici.")
    key = f"report-{ev['id']}"
    if st.button("Préparer un brouillon à partir des données", key=f"draft-{ev['id']}"):
        st.session_state[key] = (
            f"Bilan {ev['name']} — {fr_date(ev['date'])}\n\n"
            f"Invités : {invited} · Inscrits : {k['registrations']} · Confirmés : {k['accepted']} · "
            f"Refus : {k['refused']} · Sans réponse : {k['no_answer']} (taux de réponse {rate}).\n"
            f"Préparation : {k['tasks_done']}/{k['tasks_total']} tâches terminées.\n\n"
            "Points positifs :\n- \n\nDifficultés :\n- \n\nSuivis à réaliser :\n- ")
    text = st.text_area("Bilan et suivi post-événement", value=st.session_state.get(key, ev["report"] or ""), height=260, key=f"ta-{key}")
    if st.button("Enregistrer le bilan", key=f"save-report-{ev['id']}"):
        store.update_event(cx, ev["id"], report=text)
        st.session_state[key] = text
        st.success("Bilan enregistré.")


def page_event(cx, event_id: int):
    ev = store.get_event(cx, event_id)
    if not ev:
        st.warning("Cet événement n'existe plus.")
        return
    j = f" · J-{(ev['date'] - TODAY).days}" if ev["date"] and ev["date"] >= TODAY else ""
    st.markdown(f"### {ev['name']} · {fr_date(ev['date'])}{j}" + (f" · {ev['venue']}" if ev["venue"] else ""))
    names = ["Vue d'ensemble", "Participants", "Staff et intervenants", "Logistique", "Communications",
             "Automatisations", "Historique", "Bilan et suivi post-événement"]
    fns = (section_overview, tab_people, tab_staff, tab_documents, tab_comms, section_automations, tab_journal, section_report)
    for tab, fn in zip(st.tabs(names), fns):
        with tab:
            fn(cx, ev)

def tab_details(cx, ev):
    with st.form(f"details-{ev['id']}"):
        c1, c2, c3 = st.columns(3)
        when = c1.date_input("Date", value=ev["date"] or TODAY, format="DD/MM/YYYY")
        venue = c2.text_input("Lieu (salle)", ev["venue"] or "")
        dr = c3.text_input("Responsable (DR)", ev["dr"] or "")
        c1, c2, c3 = st.columns(3)
        contact = c1.text_input("Contact sur place", ev["contact_name"] or "")
        phone = c2.text_input("Téléphone", ev["contact_phone"] or "")
        mail = c3.text_input("E-mail", ev["contact_email"] or "")
        c1, c2, c3, c4 = st.columns(4)
        price = c1.number_input("Prix (€)", min_value=0.0, value=float(ev["price"] or 0.0), step=50.0)
        indoor = c2.number_input("Terrains indoor", min_value=0, value=int(ev["indoor_courts"] or 0))
        outdoor = c3.number_input("Terrains outdoor", min_value=0, value=int(ev["outdoor_courts"] or 0))
        capacity = c4.number_input("Capacité / prévu", min_value=0, value=int(ev["capacity"] or 0))
        status = st.selectbox("Statut", list(EVENT_STATUS), index=list(EVENT_STATUS).index(ev["status"])
                              if ev["status"] in EVENT_STATUS else 0, format_func=EVENT_STATUS.get)
        notes = st.text_area("Notes", ev["notes"] or "")
        if st.form_submit_button("Enregistrer"):
            store.update_event(cx, ev["id"], date=when, venue=venue, dr=dr, contact_name=contact, contact_phone=phone,
                               contact_email=mail, price=price or None, indoor_courts=indoor or None,
                               outdoor_courts=outdoor or None, capacity=capacity or None, status=status, notes=notes)
            st.success("Enregistré.")
            rerun(cx)


def tab_tasks(cx, ev):
    tl = store.list_tasks(cx, ev["id"])
    soon = get_settings().get("digest", "soon_days", default=7)
    st.progress(progress_of(tl), text=f"{sum(t['status'] == 'done' for t in tl)} / {len(tl)} tâches terminées")
    flag = {"done": "✅", "late": "🔴 en retard", "soon": "🟠 bientôt", "later": "", "undated": ""}
    df = pd.DataFrame([{"id": t["id"], "Tâche": t["name"], "Catégorie": t["category"], "Échéance": t["deadline"],
                        "Alerte": flag[store.classify(t["status"], t["deadline"], TODAY, soon)],
                        "Statut": TASK_LABELS[t["status"]]} for t in tl])
    if df.empty:
        st.info("Aucune tâche.")
    else:
        edited = st.data_editor(df, hide_index=True, key=f"tasks-{ev['id']}", disabled=["id", "Tâche", "Catégorie", "Échéance", "Alerte"],
                                column_order=["Tâche", "Catégorie", "Échéance", "Alerte", "Statut"],
                                column_config={"Statut": st.column_config.SelectboxColumn("Statut", options=list(TASK_LABELS.values()), required=True),
                                               "Échéance": st.column_config.DateColumn("Échéance", format="DD/MM/YYYY")})
        back = {v: k for k, v in TASK_LABELS.items()}
        changes = [(int(r["id"]), back[r["Statut"]]) for _, r in edited.iterrows()
                   if back[r["Statut"]] != next(t["status"] for t in tl if t["id"] == r["id"])]
        if changes and st.button(f"Enregistrer {len(changes)} changement(s)", key=f"save-tasks-{ev['id']}"):
            for tid, status in changes:
                store.set_task_status(cx, tid, status)
            rerun(cx)
    if st.button("Ajouter les tâches du référentiel manquantes", key=f"ensure-{ev['id']}"):
        n = store.ensure_tasks(cx, ev["id"], get_settings().templates())
        st.success(f"{n} tâche(s) ajoutée(s).")
        rerun(cx)


def _people_views(cx, ev, regs):
    """Deux vues des inscrits : tableau complet filtrable (équipe en 1re colonne) et vue par équipe."""
    names = store.team_names(regs)
    label = lambda r: names.get(r["id"], "— seul(e)")  # noqa: E731
    v_all, v_teams = st.tabs(["Inscrits", "Par équipe"])
    with v_all:
        df = pd.DataFrame([{"id": r["id"], "Équipe": label(r), "Prénom": r["prenom"], "Nom": r["nom"], "Entreprise": r["company"],
                            "E-mail": r["email"], "Téléphone": r["phone"], "Type": r["type"], "Niveau": r["level"],
                            "Enjeux": r["stakes"], "Poule": r["poule"], "T-shirt": r["size"],
                            "Invitation": INVITE_LABELS[r["invite_status"]]} for r in regs])
        cols = [c for c in df.columns if c != "id"]
        shown = st.multiselect("Colonnes affichées", cols, default=cols, key=f"cols-{ev['id']}")
        with st.expander("Filtrer les lignes"):
            f1, f2, f3 = st.columns(3)
            query = f1.text_input("Recherche (toutes colonnes)", key=f"q-{ev['id']}")
            picks = {}
            for widget, col in ((f2, "Équipe"), (f3, "Entreprise"), (f1, "Type"), (f2, "Niveau"), (f3, "Poule"), (f1, "Invitation")):
                opts = sorted({v for v in df[col] if v not in (None, "")})
                picks[col] = widget.multiselect(col, opts, key=f"f-{col}-{ev['id']}")
        view = df
        if query.strip():
            q = query.strip().lower()
            view = view[view[cols].astype(str).apply(lambda row: q in " ".join(row).lower(), axis=1)]
        for col, chosen in picks.items():
            if chosen:
                view = view[view[col].isin(chosen)]
        if len(view) != len(df):
            st.caption(f"{len(view)} ligne(s) sur {len(df)}.")
        st.caption("✏️ Cliquez dans une cellule pour la modifier ; la ligne vide en bas du tableau (ou le « + ») ajoute un participant ; "
                   "sélectionnez une ligne puis la corbeille pour la retirer. Rien n'est enregistré avant « Enregistrer ». "
                   "Vos modifications ne sont plus écrasées par la synchronisation Typeform.")
        sig = abs(hash((query, tuple(sorted((k, tuple(v)) for k, v in picks.items())), tuple(shown), len(regs))))
        types = sorted({"Tournoi", "Initiation", "Cocktail", *[t for t in df["Type"] if t]})
        sizes = ["XS", "S", "M", "L", "XL", "XXL"]
        edited = st.data_editor(
            view, hide_index=True, num_rows="dynamic", key=f"regs-{ev['id']}-{sig}", column_order=list(shown),
            disabled=["id", "Équipe"],
            column_config={"Invitation": st.column_config.SelectboxColumn("Invitation", options=list(INVITE_LABELS.values())),
                           "Type": st.column_config.SelectboxColumn("Type", options=types),
                           "T-shirt": st.column_config.SelectboxColumn("T-shirt", options=sizes),
                           "Enjeux": st.column_config.TextColumn("Enjeux", width="large")})
        back = {v: k for k, v in INVITE_LABELS.items()}
        by_id = {int(r["id"]): r for _, r in view.iterrows()}
        clean = lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()  # noqa: E731
        person_cols = {"Prénom": "prenom", "Nom": "nom", "Entreprise": "company", "E-mail": "email", "Téléphone": "phone"}
        reg_cols = {"Type": "type", "Niveau": "level", "Enjeux": "stakes", "Poule": "poule", "T-shirt": "size"}
        edits, adds, kept = [], [], set()
        for _, r in edited.iterrows():
            if pd.isna(r.get("id")):
                if clean(r["Prénom"]) or clean(r["Nom"]) or clean(r["Entreprise"]):
                    adds.append(r)
                continue
            rid = int(r["id"])
            kept.add(rid)
            old = by_id.get(rid)
            if old is None:
                continue
            person = {f: clean(r[c]) for c, f in person_cols.items() if clean(r[c]) != clean(old[c])}
            reg = {f: clean(r[c]) for c, f in reg_cols.items() if clean(r[c]) != clean(old[c])}
            status = back.get(clean(r["Invitation"])) if clean(r["Invitation"]) != clean(old["Invitation"]) else None
            if person or reg or status:
                edits.append((rid, person, reg, status))
        removed = [rid for rid in by_id if rid not in kept]
        if edits or adds or removed:
            st.info(f"À enregistrer : {len(edits)} modification(s), {len(adds)} ajout(s), {len(removed)} retrait(s).")
            ok_rm = True
            if removed:
                ok_rm = st.checkbox(f"Je confirme le retrait de {len(removed)} inscription(s) (venues du formulaire : ignorées ; saisies à la main : supprimées)",
                                    key=f"rm-ok-{ev['id']}-{sig}")
            if st.button("Enregistrer les modifications", key=f"save-regs-{ev['id']}", disabled=not ok_rm):
                for rid, person, reg, status in edits:
                    store.update_participant(cx, rid, person, {**reg, **({"invite_status": status} if status else {})})
                for r in adds:
                    store.add_participant(cx, ev["id"], invite_status=back.get(clean(r["Invitation"]), "a_inviter"),
                                          prenom=clean(r["Prénom"]), nom=clean(r["Nom"]), company=clean(r["Entreprise"]),
                                          email=clean(r["E-mail"]), phone=clean(r["Téléphone"]), type=clean(r["Type"]) or "Tournoi",
                                          level=clean(r["Niveau"]), stakes=clean(r["Enjeux"]), poule=clean(r["Poule"]), size=clean(r["T-shirt"]))
                for rid in removed:
                    store.remove_registration(cx, rid)
                flash(f"{len(edits)} modification(s), {len(adds)} ajout(s), {len(removed)} retrait(s) enregistré(s).")
                rerun(cx)
        with st.expander("Former ou défaire une équipe à la main"):
            opts = {r["id"]: f"{r['prenom']} {r['nom']} · {r['company'] or '—'} · {label(r)}" for r in regs}
            duo = st.multiselect("Deux joueurs à réunir en équipe", list(opts), format_func=lambda i: opts[i], max_selections=2, key=f"duo-{ev['id']}")
            c1, c2 = st.columns(2)
            if c1.button("Former l'équipe", key=f"duo-go-{ev['id']}", disabled=len(duo) != 2):
                a_, b_ = (next(r for r in regs if r["id"] == i) for i in duo)
                store.set_team(cx, duo, f"{a_['nom'] or a_['prenom']} / {b_['nom'] or b_['prenom']}")
                flash("Équipe formée.")
                rerun(cx)
            if c2.button("Défaire l'équipe des joueurs sélectionnés", key=f"duo-undo-{ev['id']}", disabled=not duo):
                store.set_team(cx, duo, "")
                flash("Équipe défaite (les joueurs redeviennent seuls).")
                rerun(cx)
    with v_teams:
        rows = [{"Équipe": names[r["id"]], "Nom": r["nom"], "Prénom": r["prenom"], "Entreprise": r["company"]}
                for r in sorted(regs, key=lambda r: (r["id"] if r["id"] in names else 10**9)) if r["id"] in names]
        rows.sort(key=lambda d: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", d["Équipe"])])
        solos = [{"Équipe": "— seul(e), à apparier —", "Nom": r["nom"], "Prénom": r["prenom"], "Entreprise": r["company"]}
                 for r in regs if r["id"] not in names and (r["type"] or "Tournoi").lower() != "cocktail"]
        st.caption(f"{len({d['Équipe'] for d in rows})} équipe(s) de 2 · {len(solos)} inscrit(s) seul(s) à apparier "
                   "(l'appariement par niveau se fait dans « Logistique » → Calculer poules et planning).")
        st.dataframe(pd.DataFrame(rows + solos, columns=["Équipe", "Nom", "Prénom", "Entreprise"]), hide_index=True)


def tab_people(cx, ev):
    regs = store.list_registrations(cx, ev["id"])
    k = store.participation(regs, ev["capacity"])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Participants", k["participants"], help="Hors cocktail et refus")
    c2.metric("Équipes de 2", k["teams"])
    c3.metric("Inscrits seuls", k["solos"], help="Appariés par niveau à la constitution des poules")
    c4.metric("Équipes au total", k["teams_total"], help="Une fois les inscrits seuls appariés" + (" (un joueur resterait sans coéquipier)" if k["odd"] else ""))
    if ev["capacity"]:
        st.caption(f"Places restantes : **{k['places_left']}** sur {ev['capacity']}.")
    if not regs:
        st.info("Aucun inscrit enregistré pour cet événement. Synchronisez Typeform (bloc ci-dessous) ou importez un fichier.")
    else:
        _people_views(cx, ev, regs)
    with st.expander("Connexion Typeform (inscriptions automatiques)", expanded=not ev["typeform_id"]):
        tid = st.text_input("Identifiant du formulaire Typeform", value=ev["typeform_id"] or "", key=f"tfid-{ev['id']}",
                            help="La fin de l'adresse du formulaire : https://xxx.typeform.com/to/IDENTIFIANT")
        if tid.strip() != (ev["typeform_id"] or "") and st.button("Enregistrer l'identifiant", key=f"tfid-save-{ev['id']}"):
            store.update_event(cx, ev["id"], typeform_id=tid.strip())
            rerun(cx)
        if not os.environ.get("TYPEFORM_TOKEN"):
            st.warning("Aucun jeton Typeform : ajoutez `TYPEFORM_TOKEN` dans les Secrets Streamlit (et dans les secrets GitHub "
                       "pour la synchro automatique toutes les 15 minutes).")
        elif ev["typeform_id"]:
            b1, b2 = st.columns(2)
            if b1.button("Simuler la synchronisation", key=f"tf-sim-{ev['id']}"):
                try:
                    rep = importer.sync_typeform(cx, ev["name"], get_settings().typeform_client(), get_settings(), dry_run=True)
                    st.info("Simulation : " + rep.line())
                    for w in rep.warnings:
                        st.warning(w)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Typeform : {exc}")
            if b2.button("Synchroniser maintenant", key=f"tf-go-{ev['id']}"):
                try:
                    rep = importer.sync_typeform(cx, ev["name"], get_settings().typeform_client(), get_settings())
                    st.session_state[f"tf-last-{ev['id']}"] = (f"{datetime.now():%H:%M:%S} · " + rep.line(), list(rep.warnings))
                    flash(rep.line())
                    rerun(cx)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Typeform : {exc}")
        last = st.session_state.get(f"tf-last-{ev['id']}")
        if last:
            st.info("Dernière synchronisation : " + last[0])
            for w in last[1]:
                st.warning(w)
        n_form = sum(1 for r in regs if r["source"] == "formulaire")
        if n_form:
            st.caption(f"{n_form} inscription(s) viennent déjà du formulaire. Si elles ont été importées avec une ancienne version "
                       "(noms vides, mauvaises colonnes), supprimez-les puis resynchronisez.")
            ok = st.checkbox(f"Je confirme : supprimer ces {n_form} inscription(s) issues du formulaire pour {ev['name']}", key=f"tf-reset-ok-{ev['id']}")
            if st.button("Supprimer et repartir de zéro", key=f"tf-reset-{ev['id']}", disabled=not ok):
                a, b = store.reset_form_signups(cx, ev["id"])
                flash(f"{a} inscription(s) supprimée(s). Cliquez maintenant sur « Synchroniser maintenant ».")
                rerun(cx)
        if os.environ.get("TYPEFORM_TOKEN") and ev["typeform_id"] and st.button("Diagnostic du formulaire (sans données personnelles)", key=f"tf-diag-{ev['id']}"):
            try:
                from padel.signups import typeform_diagnostic
                cl = get_settings().typeform_client()
                st.code("\n".join(typeform_diagnostic(cl.get_form(ev["typeform_id"].strip()), list(cl.iter_responses(ev["typeform_id"].strip())), get_settings())))
            except Exception as exc:  # noqa: BLE001
                st.error(f"Typeform : {exc}")
        st.caption("Une fois l'identifiant et le jeton en place, le moteur récupère seul les nouvelles inscriptions toutes les 15 minutes.")
    with st.expander("Importer un fichier CSV (Google Sheet) à la place", expanded=False):
        st.caption("Exportez le Google Sheet en CSV (Fichier → Télécharger → .csv). Une ligne = un inscrit seul, ou deux joueurs "
                   "si un coéquipier est renseigné. Rejouable sans doublon : à chaque nouvel export, seuls les nouveaux inscrits sont ajoutés.")
        up = st.file_uploader("Fichier CSV", type=["csv"], key=f"signup-up-{ev['id']}")
        if up is not None:
            data = up.getvalue()
            sim = importer.import_signups(cx, data, ev["name"], get_settings(), dry_run=True)
            st.info("Simulation : " + sim.line())
            for w in sim.warnings:
                st.warning(w)
            if st.button("Importer ces inscriptions", key=f"signup-go-{ev['id']}"):
                rep = importer.import_signups(cx, data, ev["name"], get_settings())
                flash(rep.line())
                rerun(cx)
    with st.expander("Ignorer une inscription (test, doublon…)"):
        st.caption("L'inscription disparaît de la liste et des compteurs, et les prochaines synchronisations Typeform ne la recréent pas. "
                   "Elle reste dans Typeform ; vous pouvez la rétablir.")
        if regs:
            opts = {r["id"]: f"{r['prenom']} {r['nom']} · {r['company'] or '—'} · {r['email'] or 'sans e-mail'}" for r in regs}
            pick_id = st.selectbox("Inscription à ignorer", list(opts), format_func=lambda i: opts[i], key=f"ign-pick-{ev['id']}")
            why = st.text_input("Motif (facultatif)", placeholder="ex. test", key=f"ign-why-{ev['id']}")
            if st.button("Ignorer cette inscription", key=f"ign-go-{ev['id']}"):
                store.ignore_registration(cx, int(pick_id), why)
                flash("Inscription ignorée.")
                rerun(cx)
        for ig in store.list_ignored(cx, ev["id"]):
            c1, c2 = st.columns([4, 1])
            c1.write(f"🚫 {ig['label']}" + (f" — {ig['reason']}" if ig["reason"] else ""))
            if c2.button("Rétablir", key=f"ign-undo-{ig['id']}"):
                store.unignore(cx, ig["id"])
                flash(f"{ig['label']} rétabli(e) : revient à la prochaine synchronisation.")
                rerun(cx)
    with st.expander("Ajouter un participant"):
        with st.form(f"add-person-{ev['id']}", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            prenom, nom, mail = c1.text_input("Prénom"), c2.text_input("Nom"), c3.text_input("E-mail")
            c1, c2, c3 = st.columns(3)
            company, team = c1.text_input("Entreprise"), c2.text_input("Équipe")
            typ = c3.selectbox("Type", ["Tournoi", "Initiation", "Cocktail"])
            if st.form_submit_button("Ajouter") and (prenom or nom):
                pid, _ = store.upsert_person(cx, prenom=prenom, nom=nom, email=mail, company=company, source="saisie")
                store.upsert_registration(cx, ev["id"], pid, type=typ, team=team, source="saisie")
                store.log(cx, "person_added", ev["id"], f"{prenom} {nom}", "utilisateur")
                rerun(cx)


def tab_staff(cx, ev):
    from padel import staff_sheet
    from padel.staff_template import staff_template_xlsx
    settings = get_settings()
    roles = settings.get("staff", "roles", default=[]) or []
    rows = store.list_staff(cx, ev["id"])
    sm = store.staff_summary(rows, roles)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Inscrits staff", sm["total"])
    c2.metric("Déjeuner : oui", sm["lunch"])
    c3.metric("Rôle à attribuer", sm["unassigned"], help="Inscrits dont la colonne « Rôle » est vide")
    c4.metric("Polos", sum(sm["sizes"].values()), help=", ".join(f"{k} : {v}" for k, v in sorted(sm["sizes"].items())) or "Aucune taille renseignée")
    if roles and rows:
        table = []
        for r in roles:
            got, want = sm["by_role"][r["name"]], r.get("target")
            table.append({"Rôle": r["name"], "Attribués": got, "Visés": "—" if want is None else want,
                          "Statut": "—" if want is None else ("✅ complet" if got >= want else f"🟠 manque {want - got}")})
        if sm["other"]:
            table.append({"Rôle": "Autre (texte libre)", "Attribués": sm["other"], "Visés": "—", "Statut": "—"})
        st.dataframe(pd.DataFrame(table), hide_index=True)
    if store.get_setting(cx, "staff_typeform_id"):
        st.caption("📝 Les réponses du formulaire Typeform du staff (choix « Événement » = " + ev["name"] + ") arrivent ici toutes les 15 minutes.")
    with st.expander("Google Sheet d'inscription du staff", expanded=False):
        url = st.text_input("Lien du Google Sheet", value=ev["staff_sheet_url"] or "", key=f"ss-url-{ev['id']}",
                            placeholder="https://docs.google.com/spreadsheets/d/…",
                            help="Partagez le fichier : Partager → Accès général → « Tous les utilisateurs disposant du lien » (Lecteur). "
                                 "L'app ne fait que le lire.")
        if url.strip() != (ev["staff_sheet_url"] or "") and st.button("Enregistrer le lien", key=f"ss-save-{ev['id']}"):
            try:
                staff_sheet.parse_sheet_url(url)
                store.update_event(cx, ev["id"], staff_sheet_url=url.strip())
                rerun(cx)
            except staff_sheet.SheetError as exc:
                st.error(str(exc))
        if ev["staff_sheet_url"]:
            b1, b2 = st.columns(2)
            for col, label, dry in ((b1, "Simuler la synchronisation", True), (b2, "Synchroniser maintenant", False)):
                if col.button(label, key=f"ss-{'sim' if dry else 'go'}-{ev['id']}"):
                    try:
                        r = staff_sheet.sync_staff(cx, ev["id"], staff_sheet.fetch_csv(ev["staff_sheet_url"]), dry_run=dry, actor="utilisateur")
                        msg = (f"{r['members']} personne(s) dans le fichier : {r['added']} ajoutée(s), {r['updated']} mise(s) à jour, "
                               f"{r['removed']} retirée(s).")
                        if dry:
                            st.info("Simulation : " + msg)
                            for w in r["warnings"]:
                                st.warning(w)
                        else:
                            st.session_state[f"ss-last-{ev['id']}"] = (f"{datetime.now():%H:%M:%S} · " + msg, r["warnings"])
                            flash(msg)
                            rerun(cx)
                    except staff_sheet.SheetError as exc:
                        st.error(str(exc))
            last = st.session_state.get(f"ss-last-{ev['id']}")
            if last:
                st.info("Dernière synchronisation : " + last[0])
                for w in last[1]:
                    st.warning(w)
            st.caption("Une fois le lien enregistré, le moteur relit le fichier toutes les 15 minutes : ajouts, changements de rôle, de polo ou de déjeuner. "
                       "Les membres ajoutés à la main ci-dessous ne sont jamais supprimés.")
        st.download_button("⬇ Modèle de fichier staff (à importer dans Google Drive)",
                           staff_template_xlsx(ev["name"], ev["date"], ev["venue"] or "", roles),
                           file_name=f"STAFF-{slug(ev['name'])}.xlsx", key=f"ss-tpl-{ev['id']}",
                           help="Drive → Nouveau → Importer un fichier → ouvrir avec Google Sheets → Partager par lien (Lecteur) → coller le lien ici.")
    df = pd.DataFrame(rows or [{k: "" for k in store.STAFF_FIELDS}], columns=list(store.STAFF_FIELDS))
    labels = {"nom": "Nom", "prenom": "Prénom", "role": "Rôle", "shirt_size": "Taille t-shirt", "hotel": "Hôtel",
              "transport": "Transport", "repas": "Repas"}
    edited = st.data_editor(df.rename(columns=labels), num_rows="dynamic", hide_index=True, key=f"staff-{ev['id']}")
    if st.button("Enregistrer le staff", key=f"save-staff-{ev['id']}"):
        back = {v: k for k, v in labels.items()}
        n = store.save_staff(cx, ev["id"], edited.rename(columns=back).fillna("").to_dict("records"))
        st.success(f"{n} membre(s) enregistré(s).")


def _with_poules(parts, plan):
    """Copie des participants avec leur poule calculée (pour imprimer la poule sur l'étiquette)."""
    from dataclasses import replace
    where = {m.id: po.name for po in plan.poules for t in po.teams for m in t.members}
    return [replace(p, poule=where.get(p.id, p.poule)) for p in parts]


def tab_documents(cx, ev):
    from padel.documents import diplomas_pdf, labels_pdf, planning_pdf, poules_pdf, referee_pdf
    from padel.documents_docx import labels_docx, tournament_csv, tournament_xlsx
    from padel.logistics import logistics_report
    from padel.participants import Participant, active_participants
    from padel.tournament import plan_tournament
    settings = get_settings()
    status_label = {"a_inviter": "Not started", "invite": "Pas de réponse", "accepte": "Acceptée", "refuse": "Refusée",
                    "erreur": "Erreur mail"}
    regs = store.list_registrations(cx, ev["id"])
    tnames = store.team_names(regs)   # nom d'équipe = entreprise (« Entreprise 2 » en cas d'homonymie)
    parts = [Participant(id=str(r["id"]), prenom=r["prenom"], nom=r["nom"], email=r["email"], telephone=r["phone"],
                         company=r["company"], team=tnames.get(r["id"], ""), level=r["level"], size=r["size"], type=r["type"],
                         invite_status=status_label[r["invite_status"]], poule=r["poule"]) for r in regs]
    if not ev["date"]:
        st.warning("Renseignez la date de l'événement.")
        return
    c1, c2, c3 = st.columns(3)
    courts = c1.number_input("Terrains", 1, 12, int(ev["courts"] or settings.get("tournament", "courts", default=4)), key=f"co-{ev['id']}")
    minutes = c2.number_input("Minutes par match", 5, 60, int(settings.get("tournament", "match_minutes", default=20)), key=f"mi-{ev['id']}")
    size = c3.number_input("Équipes par poule", 3, 8, int(settings.get("tournament", "poule_size", default=4)), key=f"ps-{ev['id']}")
    if st.button("Calculer poules et planning", key=f"plan-{ev['id']}"):
        plan = plan_tournament(parts, settings, ev["date"], size, courts, minutes)
        out = settings.out_dir / slug(ev["name"])
        files = {"planning.pdf": planning_pdf(out / "planning.pdf", settings.brand, ev["name"], ev["date"], plan),
                 "poules.pdf": poules_pdf(out / "poules.pdf", settings.brand, ev["name"], plan),
                 "arbitres.pdf": referee_pdf(out / "arbitres.pdf", settings.brand, ev["name"], plan),
                 "diplomes.pdf": diplomas_pdf(out / "diplomes.pdf", settings.brand, ev["name"], ev["date"], active_participants(parts, settings)),
                 "etiquettes.pdf": labels_pdf(out / "etiquettes.pdf", settings.brand, ev["name"], active_participants(parts, settings))}
        word = labels_docx(out / "etiquettes-avery-L7160.docx", settings.brand, ev["name"], active_participants(_with_poules(parts, plan), settings))
        files["etiquettes-avery-L7160.docx"] = word
        st.session_state[f"tool-{ev['id']}"] = {"equipes-poules.csv": tournament_csv(plan), "equipes-poules.xlsx": tournament_xlsx(plan)}
        st.session_state[f"poules-map-{ev['id']}"] = {int(m.id): po.name for po in plan.poules for t in po.teams for m in t.members}
        st.session_state[f"docs-{ev['id']}"] = {n: open(p, "rb").read() for n, p in files.items()}
        st.session_state[f"plan-info-{ev['id']}"] = (f"{len(plan.teams)} équipes · {len(plan.poules)} poule(s) · {len(plan.matches)} matchs",
                                                     list(plan.warnings))
    docs = st.session_state.get(f"docs-{ev['id']}")
    if docs:
        tool = st.session_state.get(f"tool-{ev['id']}", {})
        st.caption("Export pour l'outil de tournoi (une ligne par équipe) :")
        for col, (name, data) in zip(st.columns(max(len(tool), 1)), tool.items()):
            col.download_button(f"⬇ {name}", data, file_name=f"{slug(ev['name'])}-{name}", key=f"dlt-{ev['id']}-{name}")
        if st.button("Enregistrer ces poules dans les fiches participants", key=f"save-poules-{ev['id']}"):
            n = store.set_poules(cx, ev["id"], st.session_state[f"poules-map-{ev['id']}"])
            flash(f"{n} inscrit(s) affecté(s) à une poule.")
            rerun(cx)
        info, warns = st.session_state[f"plan-info-{ev['id']}"]
        st.success(info)
        for w in warns:
            st.warning(w)
        cols = st.columns(len(docs))
        for col, (name, data) in zip(cols, docs.items()):
            col.download_button(f"⬇ {name}", data, file_name=f"{slug(ev['name'])}-{name}", key=f"dl-{ev['id']}-{name}")
    st.divider()
    if st.button("Calculer t-shirts et repas", key=f"logi-{ev['id']}"):
        from padel.events import Event
        from padel.logistics import StaffMember
        sm = [StaffMember(name=s["nom"], prenom=s["prenom"], role=s["role"], shirt=s["shirt_size"], hotel=s["hotel"],
                          transport=s["transport"], meal=s["repas"]) for s in store.list_staff(cx, ev["id"])]
        report = logistics_report(settings, Event(id=str(ev["id"]), name=ev["name"], date=ev["date"], venue=ev["venue"] or ""), parts, sm)
        st.json(report)


def tab_comms(cx, ev):
    rows = cx.execute(select(communications).where(communications.c.event_id == ev["id"]).order_by(communications.c.id.desc())).mappings().all()
    if not rows:
        st.caption("Aucune communication préparée. Le moteur en prépare lorsque l'invitation ou la relance devient due.")
    for r in rows:
        with st.expander(f"{KIND_LABELS.get(r['kind'], r['kind'])} → {r['to_addr']} · {r['status']}"):
            st.text(f"Objet : {r['subject']}\n\n{r['body']}")


def tab_journal(cx, ev):
    rows = cx.execute(select(action_log).where(action_log.c.event_id == ev["id"]).order_by(action_log.c.id.desc()).limit(200)).mappings().all()
    st.dataframe(pd.DataFrame([{"Date": fmt_dt(r["ts"]), "Par": r["actor"], "Action": r["action"], "Détail": r["detail"]} for r in rows]), hide_index=True)


def page_people(cx):
    regs = store.list_registrations(cx)
    if not regs:
        st.info("Aucun participant. Importez vos fichiers dans « Données ».")
        return
    df = pd.DataFrame([{"Événement": r["event_name"], "Prénom": r["prenom"], "Nom": r["nom"], "Entreprise": r["company"],
                        "E-mail": r["email"], "Téléphone": r["phone"], "Type": r["type"], "Équipe": r["team"],
                        "Invitation": INVITE_LABELS[r["invite_status"]]} for r in regs])
    c1, c2, c3 = st.columns(3)
    ev = c1.multiselect("Événement", sorted(df["Événement"].unique()))
    stt = c2.multiselect("Invitation", sorted(df["Invitation"].unique()))
    q = c3.text_input("Recherche (nom, entreprise, e-mail)")
    if ev:
        df = df[df["Événement"].isin(ev)]
    if stt:
        df = df[df["Invitation"].isin(stt)]
    if q:
        mask = df.apply(lambda r: q.lower() in " ".join(map(str, r.values)).lower(), axis=1)
        df = df[mask]
    st.caption(f"{len(df)} ligne(s)")
    st.dataframe(df, hide_index=True)



def page_automation(cx):
    st.write("Le moteur prépare des **aperçus** et **n'envoie rien**. Relisez, envoyez vous-même, puis cliquez "
             "« Marquer comme envoyé » en attendant le connecteur Outlook.")
    c1, c2, c3 = st.columns(3)
    if c1.button("Simuler un passage (n'écrit rien)"):
        st.info(automation.run(get_engine(), get_settings(), TODAY, dry_run=True, trigger="manuel").summary())
    if c2.button("Lancer un passage maintenant"):
        res = automation.run(get_engine(), get_settings(), TODAY, trigger="manuel")
        (st.error if res.errors else st.success)(res.summary())
        for n in res.notes:
            st.warning(n)
    c3.caption("Passage automatique : toutes les 15 min via GitHub Actions (workflow « padel-engine »).")
    st.subheader("Communications préparées")
    status = st.multiselect("Statut", ["preview", "approved", "sent", "cancelled"], default=["preview", "approved"])
    rows = cx.execute(select(communications, t_events.c.name.label("event_name")).join(
        t_events, t_events.c.id == communications.c.event_id, isouter=True)
        .where(communications.c.status.in_(status)).order_by(communications.c.id.desc()).limit(100)).mappings().all()
    if not rows:
        st.caption("Aucune communication dans ce filtre.")
    for r in rows:
        with st.expander(f"{r['event_name']} · {KIND_LABELS.get(r['kind'], r['kind'])} → {r['to_addr']} · {r['status']}"):
            st.text(f"Objet : {r['subject']}\n\n{r['body']}")
            b1, b2, _ = st.columns(3)
            if r["status"] in ("preview", "approved"):
                if b1.button("Marquer comme envoyé", key=f"sent-{r['id']}"):
                    store.mark_sent(cx, r["id"])
                    rerun(cx)
                if b2.button("Annuler", key=f"cancel-{r['id']}"):
                    store.set_communication_status(cx, r["id"], "cancelled")
                    rerun(cx)


def page_validate(cx):
    st.caption("Uniquement les cas qui nécessitent réellement une décision humaine.")
    rows = cx.execute(select(pending, t_events.c.name.label("event_name")).join(
        t_events, t_events.c.id == pending.c.event_id, isouter=True)
        .where(pending.c.status.in_(("open", "postponed"))).order_by(pending.c.id)).mappings().all()
    if not rows:
        st.success("Rien à valider.")
    for r in rows:
        with st.container(border=True):
            st.markdown(f"**{r['title']}**" + (" _(reporté)_" if r["status"] == "postponed" else ""))
            st.caption(f"Pourquoi : {r['reason']}")
            b1, b2, b3 = st.columns(3)
            for col, (label, status) in zip((b1, b2, b3), (("Accepter", "accepted"), ("Rejeter", "rejected"), ("Reporter", "postponed"))):
                if col.button(label, key=f"{status}-{r['id']}"):
                    store.resolve_pending(cx, r["id"], status)
                    rerun(cx)


def page_history(cx):
    evs = {e["id"]: e["name"] for e in store.list_events(cx)}
    c1, c2, c3 = st.columns(3)
    pick_ev = c1.multiselect("Événement", sorted(evs.values()))
    actor = c2.multiselect("Par", ["systeme", "utilisateur", "import"])
    q = c3.text_input("Recherche")
    rows = cx.execute(select(action_log).order_by(action_log.c.id.desc()).limit(2000)).mappings().all()
    df = pd.DataFrame([{"Date": fmt_dt(r["ts"]), "Par": r["actor"], "Événement": evs.get(r["event_id"], ""),
                        "Action": r["action"], "Détail": r["detail"]} for r in rows])
    if df.empty:
        st.info("Aucune action enregistrée pour le moment.")
        return
    if pick_ev:
        df = df[df["Événement"].isin(pick_ev)]
    if actor:
        df = df[df["Par"].isin(actor)]
    if q:
        df = df[df.apply(lambda r: q.lower() in " ".join(map(str, r.values)).lower(), axis=1)]
    st.caption(f"{len(df)} action(s) (2 000 dernières au maximum)")
    st.dataframe(df, hide_index=True)
    st.download_button("⬇ Exporter en CSV", df.to_csv(index=False), file_name="historique.csv")


def page_errors(cx):
    st.subheader("Automatisations en erreur")
    failing = [r for r in store.list_runs(cx, status="erreur", limit=300)
               if store.latest_run_status(cx, r["workflow"], r["event_id"]) == "erreur"]
    failing = list({(r["workflow"], r["event_id"]): r for r in failing}.values())
    if not failing:
        st.success("Aucune automatisation en erreur.")
    for r in failing:
        with st.container(border=True):
            st.markdown(f"**{r['event_name']} · {r['workflow']}** — {r['attempt']} échec(s) consécutif(s) "
                        f"(après {store.MAX_ATTEMPTS}, le cas passe dans « À valider »)")
            st.code((r["error"] or "").strip()[:900], language=None)
            if st.button("Réessayer maintenant", key=f"retry-{r['id']}"):
                res = automation.run(get_engine(), get_settings(), TODAY, trigger="manuel", only_event=r["event_id"])
                (st.error if res.errors else st.success)(res.summary())
    st.subheader("Synchronisations")
    conns = connections.all_connections(get_engine(), cx)
    sync = store.get_sync(cx)
    st.dataframe(pd.DataFrame([{"Connexion": c.name, "État": STATE_LABEL[c.state], "Détail": c.detail,
                                "Dernière réussite": fmt_dt(sync.get(c.key, {}).get("last_ok_at"))} for c in conns]),
                 hide_index=True)
    st.subheader("Historique des exécutions")
    runs = store.list_runs(cx, limit=100)
    if runs:
        st.dataframe(pd.DataFrame([{"Date": fmt_dt(r["started_at"]), "Événement": r["event_name"], "Workflow": r["workflow"],
                                    "Statut": r["status"], "Préparés": r["prepared"], "Erreur": (r["error"] or "").split("\n")[0][:100]}
                                   for r in runs]), hide_index=True)
    else:
        st.caption("Aucune exécution enregistrée.")


STATE_LABEL = {"ok": "🟢 OK (vérifié)", "configure": "🟡 Configuré, non fonctionnel", "erreur": "🔴 Erreur", "absent": "⚪ Non configuré"}


def page_settings(cx):
    st.subheader("Connexions")
    st.caption("Un état « OK » n'apparaît que si la connexion a été vérifiée par un appel réel. Rien n'est simulé.")
    test = st.button("Tester Typeform")
    for c in connections.all_connections(get_engine(), cx, test_typeform=test):
        with st.container(border=True):
            st.markdown(f"**{c.name}** — {STATE_LABEL[c.state]}")
            st.caption(c.detail + ("" if c.developed else "  ·  Branchement non développé dans cette version."))
            if c.needs:
                st.caption(f"À fournir : {c.needs}")
    st.subheader("Formulaire Typeform du staff (tous les événements)")
    st.caption("Un seul formulaire pour tout le staff : la question « Événement » (ville) range chaque réponse dans l'onglet Staff du bon événement.")
    fid = st.text_input("Identifiant du formulaire staff", value=store.get_setting(cx, "staff_typeform_id"), key="stf-id",
                        help="La fin de l'adresse du formulaire : https://xxx.typeform.com/to/IDENTIFIANT")
    if fid.strip() != store.get_setting(cx, "staff_typeform_id") and st.button("Enregistrer l'identifiant du formulaire staff", key="stf-save"):
        store.set_setting(cx, "staff_typeform_id", fid.strip())
        rerun(cx)
    if store.get_setting(cx, "staff_typeform_id") and not os.environ.get("TYPEFORM_TOKEN"):
        st.warning("Aucun jeton Typeform (`TYPEFORM_TOKEN` dans les Secrets).")
    elif store.get_setting(cx, "staff_typeform_id"):
        from padel import staff_sheet
        form_id = store.get_setting(cx, "staff_typeform_id")
        evs = store.list_events(cx)
        d1, d2, d3 = st.columns(3)
        try:
            if d1.button("Diagnostic du formulaire staff", key="stf-diag"):
                cl = get_settings().typeform_client()
                from padel.signups import typeform_rows  # noqa: F401
                st.code("\n".join(staff_sheet.staff_typeform_diagnostic(cl.get_form(form_id), list(cl.iter_responses(form_id)), [e["name"] for e in evs])))
            for col, label, dry in ((d2, "Simuler (tous les événements)", True), (d3, "Synchroniser (tous les événements)", False)):
                if col.button(label, key=f"stf-{'sim' if dry else 'go'}"):
                    cl, cache, lines = get_settings().typeform_client(), {}, []
                    unknown: set[str] = set()
                    for e in evs:
                        r = staff_sheet.sync_staff_typeform(cx, e, cl, form_id, dry_run=dry, cache=cache, actor="utilisateur",
                                                            event_names=[x["name"] for x in evs])
                        unknown |= set(r["unknown"])
                        if r["members"] or r["removed"]:
                            lines.append(f"{e['name']} : {r['members']} inscrit(s) staff ({r['added']} ajouté(s), {r['updated']} mis à jour, {r['removed']} retiré(s))")
                    msg = "\n".join(lines) or "Aucune réponse rattachée à un événement."
                    if dry:
                        st.info("Simulation :\n\n" + msg)
                    else:
                        flash(msg)
                    if unknown:
                        st.warning("Événement(s) non reconnu(s) dans les réponses : " + ", ".join(sorted(unknown)))
                    if not dry:
                        rerun(cx)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Typeform : {exc}")
    st.subheader("Automatisations")
    for a in store.list_automations(cx):
        c1, c2 = st.columns([4, 1])
        c1.write(f"**{a['label']}** · mode : aperçu uniquement (envoi automatique indisponible : aucun connecteur d'envoi)")
        on = c2.toggle("Actif", value=bool(a["enabled"]), key=f"auto-{a['key']}")
        if on != bool(a["enabled"]):
            store.set_automation_enabled(cx, a["key"], on)
            rerun(cx)
    st.subheader("Règles (lecture seule, modifiables dans config.yaml)")
    s = get_settings()
    rows = [{"Règle": f"Échéance « {t.name} »", "Valeur": f"J-{t.offset}"} for t in s.templates() if t.key in ("invitation", "relance")]
    rows.append({"Règle": "Délai avant relance après invitation", "Valeur": f"{s.get('engine', 'relance_after_days', default=7)} jours"})
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.subheader("Base de données")
    st.write(describe(get_engine()))

def page_data(cx):
    settings = get_settings()
    st.write(f"**Base :** {describe(get_engine())}")
    if get_engine().dialect.name == "sqlite":
        st.warning("Base locale temporaire : les données seront perdues au redémarrage. Ajoutez DATABASE_URL (Neon) dans les Secrets.")
    if st.button("Charger les 14 événements connus et leurs 22 tâches"):
        flash(importer.seed_events(cx, settings).line())
        rerun(cx)
    st.subheader("Importer un export CSV de Notion")
    st.caption("Importez d'abord les événements, puis les tâches, puis les participants. Un import est rejouable : il complète sans écraser.")
    for label, kind in (("Événements", "events"), ("Tâches", "tasks"), ("Participants", "participants"), ("Staff", "staff")):
        up = st.file_uploader(f"CSV {label.lower()}", type="csv", key=f"up-{kind}")
        default_event = None
        if kind in ("participants", "staff"):
            default_event = st.text_input("Événement" + (" (obligatoire)" if kind == "staff" else " par défaut (si le CSV n'a pas de colonne Tournoi)"),
                                          key=f"def-ev-{kind}") or None
        if up and st.button(f"Importer {label.lower()}", key=f"imp-{kind}"):
            data = up.getvalue()
            if kind == "events":
                rep = importer.import_events(cx, data)
            elif kind == "tasks":
                rep = importer.import_tasks(cx, data)
            elif kind == "staff":
                rep = importer.import_staff(cx, data, default_event or "")
            else:
                rep = importer.import_participants(cx, data, default_event, settings)
            flash(rep.line())
            for w in rep.warnings[:30]:
                flash(w, "warning")
            rerun(cx)
    st.subheader("Sauvegarde")
    if st.button("Préparer une sauvegarde (CSV de toutes les tables)"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for t in (t_events, people, registrations, t_tasks, t_staff, communications, action_log, pending):
                rows = cx.execute(select(t)).mappings().all()
                z.writestr(f"{t.name}.csv", pd.DataFrame([dict(r) for r in rows]).to_csv(index=False))
        st.download_button("⬇ sauvegarde.zip", buf.getvalue(), file_name=f"sauvegarde-padel-{TODAY:%Y%m%d}.zip")



# ----------------------------------------------------------------------------- application
st.title("🎾 Adéquat Padel Tour")
if not check_password():
    st.stop()

PAGES = {"Accueil": page_home, "Événements": page_events, "Participants": page_people,
         "Automatisations": page_automation, "À valider": page_validate, "Historique des actions": page_history,
         "Erreurs et synchronisations": page_errors, "Données": page_data, "Paramètres et connexions": page_settings}


def build_menu(cx) -> tuple[list[str], dict[str, str]]:
    """Menu construit dynamiquement : les événements viennent de la base, jamais codés en dur."""
    labels, options = {}, []
    for name in PAGES:
        options.append(f"page:{name}")
        labels[f"page:{name}"] = name
        if name == "Événements":
            for e in store.list_events(cx):
                options.append(f"event:{e['id']}")
                labels[f"event:{e['id']}"] = f"\u00a0\u00a0\u00a0\u00a0↳ {e['name']}"
    return options, labels


for _level, _msg in st.session_state.pop("flash", []):
    getattr(st, _level)(_msg)

try:
    with get_engine().connect() as _cx:
        _options, _labels = build_menu(_cx)
        if st.session_state.get("nav") not in _options:
            st.session_state["nav"] = "page:Accueil"
        with st.sidebar:
            nav = st.radio("Navigation", _options, key="nav", format_func=_labels.get, label_visibility="collapsed")
            if st.button("Se déconnecter"):
                st.session_state.clear()
                st.rerun()
        if nav.startswith("event:"):
            page_event(_cx, int(nav.split(":", 1)[1]))
        else:
            PAGES[nav.split(":", 1)[1]](_cx)
        _cx.commit()
except Exception as exc:  # noqa: BLE001
    st.error(f"Erreur : {exc}")
    st.caption("Si le message parle de connexion, vérifiez DATABASE_URL dans les Secrets.")
    if os.environ.get("PADEL_DEBUG"):
        raise
