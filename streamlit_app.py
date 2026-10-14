"""Dashboard Adéquat Padel Tour : l'application est la source de vérité (base Neon/PostgreSQL).

Secrets (Streamlit Cloud > Settings > Secrets, jamais dans le code) : APP_PASSWORD, DATABASE_URL.
Phase 1 : le moteur prépare des aperçus de communications et signale ce qui demande une décision ; aucun envoi
automatique n'est branché (Outlook/Teams viendront ensuite, après validation des autorisations).
"""
from __future__ import annotations

import hmac
import io
import os
import zipfile
from datetime import date

import pandas as pd
import streamlit as st

try:  # les secrets Streamlit deviennent des variables d'environnement
    for _k, _v in st.secrets.items():
        if isinstance(_v, str):
            os.environ.setdefault(_k, _v)
except Exception:  # pas de secrets.toml en local
    pass

from sqlalchemy import select  # noqa: E402

from padel import engine as automation, importer, store  # noqa: E402
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
def get_engine():
    engine = make_engine()
    init_db(engine)
    return engine


@st.cache_resource
def get_settings():
    return load_settings(use_dotenv=False)


def fmt_dt(value) -> str:
    return value.strftime("%d/%m/%Y %H:%M") if value else ""


def progress_of(task_list: list[dict]) -> float:
    return (sum(t["status"] == "done" for t in task_list) / len(task_list)) if task_list else 0.0


# ----------------------------------------------------------------------------- pages

def rerun(cx):
    """Valide les écritures puis relance la page (st.rerun interrompt le script : sans commit, tout serait annulé)."""
    cx.commit()
    st.rerun()


def page_home(cx):
    k = store.kpis(cx, TODAY, get_settings().get("digest", "soon_days", default=7))
    c = st.columns(6)
    c[0].metric("Événements à venir", k["events_upcoming"])
    c[1].metric("Tâches en retard", k["tasks_late"])
    c[2].metric("À faire sous 7 jours", k["tasks_soon"])
    c[3].metric("À valider", k["pending_open"])
    c[4].metric("Aperçus en attente", k["previews"])
    c[5].metric("Inscrits / acceptés", f"{k['registrations']} / {k['accepted']}")
    st.caption(f"Dernier passage du moteur : {fmt_dt(k['last_run']) or 'jamais'} · actions aujourd'hui : {k['actions_today']}")

    st.subheader("À traiter par moi")
    items = []
    for p in cx.execute(select(pending).where(pending.c.status == "open")).mappings():
        items.append({"Type": "Décision", "Sujet": p["title"], "Pourquoi": p["reason"]})
    soon = get_settings().get("digest", "soon_days", default=7)
    late = [t for t in store.list_tasks(cx) if t["event_date"] and t["event_date"] >= TODAY
            and store.classify(t["status"], t["deadline"], TODAY, soon) == "late"]
    by_event: dict[str, list[dict]] = {}
    for t in late:
        by_event.setdefault(t["event_name"], []).append(t)
    for name, ts in by_event.items():
        oldest = min(ts, key=lambda t: t["deadline"])
        items.append({"Type": "Retard", "Sujet": f"{name} : {len(ts)} tâche(s) en retard",
                      "Pourquoi": f"La plus ancienne : {oldest['name']} (échéance {fr_date(oldest['deadline'])}, "
                                  f"{(TODAY - oldest['deadline']).days} j)"})
    if items:
        st.dataframe(pd.DataFrame(items), hide_index=True)
    else:
        st.success("Rien à traiter pour le moment.")

    st.subheader("Événements")
    rows = []
    for e in store.list_events(cx):
        tl = store.list_tasks(cx, e["id"])
        regs = store.list_registrations(cx, e["id"])
        due = [t for t in tl if t["status"] != "done" and t["deadline"] and t["deadline"] < TODAY]
        rows.append({"Événement": e["name"], "Date": e["date"], "J-": (e["date"] - TODAY).days if e["date"] else None,
                     "Lieu": e["venue"], "Responsable": e["dr"], "Statut": EVENT_STATUS.get(e["status"], e["status"]),
                     "Préparation": progress_of(tl), "En retard": len(due),
                     "Inscrits": len(regs), "Acceptés": sum(r["invite_status"] == "accepte" for r in regs)})
    st.dataframe(pd.DataFrame(rows), hide_index=True, column_config={
        "Préparation": st.column_config.ProgressColumn("Préparation", min_value=0.0, max_value=1.0, format="percent"),
        "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY")})


def event_picker(cx, key: str):
    evs = store.list_events(cx)
    if not evs:
        st.info("Aucun événement : ouvrez « Données » et chargez le jeu de départ, ou créez un événement ci-dessous.")
        return None
    names = [e["name"] for e in evs]
    upcoming = [e["name"] for e in evs if e["date"] and e["date"] >= TODAY]
    default = names.index(upcoming[0]) if upcoming else 0
    choice = st.selectbox("Événement", names, index=default, key=key)
    return store.get_event(cx, choice)


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


def tab_people(cx, ev):
    regs = store.list_registrations(cx, ev["id"])
    acc = sum(r["invite_status"] == "accepte" for r in regs)
    st.caption(f"{len(regs)} inscrit(s) · {acc} accepté(s)")
    if regs:
        df = pd.DataFrame([{"id": r["id"], "Prénom": r["prenom"], "Nom": r["nom"], "Entreprise": r["company"],
                            "E-mail": r["email"], "Type": r["type"], "Équipe": r["team"], "Poule": r["poule"],
                            "T-shirt": r["size"], "Niveau": r["level"], "Invitation": INVITE_LABELS[r["invite_status"]]}
                           for r in regs])
        edited = st.data_editor(df, hide_index=True, key=f"regs-{ev['id']}", column_order=[c for c in df.columns if c != "id"],
                                disabled=[c for c in df.columns if c != "Invitation"],
                                column_config={"Invitation": st.column_config.SelectboxColumn("Invitation", options=list(INVITE_LABELS.values()), required=True)})
        back = {v: k for k, v in INVITE_LABELS.items()}
        cur = {r["id"]: r["invite_status"] for r in regs}
        changes = [(int(r["id"]), back[r["Invitation"]]) for _, r in edited.iterrows() if back[r["Invitation"]] != cur[int(r["id"])]]
        if changes and st.button(f"Enregistrer {len(changes)} statut(s)", key=f"save-regs-{ev['id']}"):
            for rid, status in changes:
                store.set_invite_status(cx, rid, status)
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
    rows = store.list_staff(cx, ev["id"])
    df = pd.DataFrame(rows or [{k: "" for k in store.STAFF_FIELDS}], columns=list(store.STAFF_FIELDS))
    labels = {"nom": "Nom", "prenom": "Prénom", "role": "Rôle", "shirt_size": "Taille t-shirt", "hotel": "Hôtel",
              "transport": "Transport", "repas": "Repas"}
    edited = st.data_editor(df.rename(columns=labels), num_rows="dynamic", hide_index=True, key=f"staff-{ev['id']}")
    if st.button("Enregistrer le staff", key=f"save-staff-{ev['id']}"):
        back = {v: k for k, v in labels.items()}
        n = store.save_staff(cx, ev["id"], edited.rename(columns=back).fillna("").to_dict("records"))
        st.success(f"{n} membre(s) enregistré(s).")


def tab_documents(cx, ev):
    from padel.documents import diplomas_pdf, labels_pdf, planning_pdf, poules_pdf, referee_pdf
    from padel.logistics import logistics_report
    from padel.participants import Participant, active_participants
    from padel.tournament import plan_tournament
    settings = get_settings()
    status_label = {"a_inviter": "Not started", "invite": "Pas de réponse", "accepte": "Acceptée", "refuse": "Refusée",
                    "erreur": "Erreur mail"}
    regs = store.list_registrations(cx, ev["id"])
    parts = [Participant(id=str(r["id"]), prenom=r["prenom"], nom=r["nom"], email=r["email"], telephone=r["phone"],
                         company=r["company"], team=r["team"], level=r["level"], size=r["size"], type=r["type"],
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
        st.session_state[f"docs-{ev['id']}"] = {n: open(p, "rb").read() for n, p in files.items()}
        st.session_state[f"plan-info-{ev['id']}"] = (f"{len(plan.teams)} équipes · {len(plan.poules)} poule(s) · {len(plan.matches)} matchs",
                                                     list(plan.warnings))
    docs = st.session_state.get(f"docs-{ev['id']}")
    if docs:
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


def page_events(cx):
    with st.expander("➕ Nouvel événement"):
        with st.form("new-event", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            name, day, venue = c1.text_input("Ville", placeholder="LILLE"), c2.date_input("Date", format="DD/MM/YYYY"), c3.text_input("Lieu")
            dr = st.text_input("Responsable (DR)")
            if st.form_submit_button("Créer avec les 22 tâches du référentiel") and name.strip():
                eid, created = store.upsert_event(cx, name, date=day, venue=venue, dr=dr)
                n = store.ensure_tasks(cx, eid, get_settings().templates())
                store.log(cx, "event_created" if created else "event_exists", eid, name.upper(), "utilisateur")
                st.success(f"{name.upper()} : {'créé' if created else 'existait déjà'}, {n} tâche(s) ajoutée(s).")
                rerun(cx)
    ev = event_picker(cx, "ev-main")
    if not ev:
        return
    st.markdown(f"### {ev['name']} · {fr_date(ev['date'])}" + (f" · {ev['venue']}" if ev["venue"] else ""))
    tabs = st.tabs(["Tâches", "Participants", "Staff", "Détails", "Documents", "Communications", "Journal"])
    for tab, fn in zip(tabs, (tab_tasks, tab_people, tab_staff, tab_details, tab_documents, tab_comms, tab_journal)):
        with tab:
            fn(cx, ev)


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
    st.write("Le moteur prépare des **aperçus** et ne **n'envoie rien**. Vous relisez, puis vous marquez comme envoyé "
             "après l'avoir envoyé vous-même, en attendant le connecteur Outlook.")
    c1, c2, c3 = st.columns(3)
    if c1.button("Simuler un passage (n'écrit rien)"):
        st.info(automation.run(get_engine(), get_settings(), TODAY, dry_run=True).summary())
    if c2.button("Lancer un passage maintenant"):
        st.success(automation.run(get_engine(), get_settings(), TODAY).summary())
        rerun(cx)
    c3.caption("Passage automatique : toutes les 15 min via GitHub Actions (workflow « engine »).")
    st.subheader("Communications préparées")
    status = st.multiselect("Statut", ["preview", "approved", "sent", "cancelled"], default=["preview", "approved"])
    rows = cx.execute(select(communications, t_events.c.name.label("event_name")).join(t_events, t_events.c.id == communications.c.event_id, isouter=True)
                      .where(communications.c.status.in_(status)).order_by(communications.c.id.desc()).limit(100)).mappings().all()
    if not rows:
        st.caption("Aucune communication dans ce filtre.")
    for r in rows:
        with st.expander(f"{r['event_name']} · {KIND_LABELS.get(r['kind'], r['kind'])} → {r['to_addr']} · {r['status']}"):
            st.text(f"Objet : {r['subject']}\n\n{r['body']}")
            b1, b2, b3 = st.columns(3)
            if r["status"] in ("preview", "approved"):
                if b1.button("Marquer comme envoyé", key=f"sent-{r['id']}"):
                    store.mark_sent(cx, r["id"])
                    rerun(cx)
                if b2.button("Annuler", key=f"cancel-{r['id']}"):
                    store.set_communication_status(cx, r["id"], "cancelled")
                    rerun(cx)
    st.subheader("Journal des actions")
    log = cx.execute(select(action_log).order_by(action_log.c.id.desc()).limit(100)).mappings().all()
    st.dataframe(pd.DataFrame([{"Date": fmt_dt(r["ts"]), "Par": r["actor"], "Action": r["action"], "Détail": r["detail"]} for r in log]), hide_index=True)


def page_validate(cx):
    rows = cx.execute(select(pending).where(pending.c.status.in_(("open", "postponed"))).order_by(pending.c.id)).mappings().all()
    if not rows:
        st.success("Rien à valider.")
    for r in rows:
        with st.container(border=True):
            st.markdown(f"**{r['title']}**" + (" _(reporté)_" if r["status"] == "postponed" else ""))
            st.caption(r["reason"])
            b1, b2, b3 = st.columns(3)
            for col, (label, status) in zip((b1, b2, b3), (("Accepter", "accepted"), ("Rejeter", "rejected"), ("Reporter", "postponed"))):
                if col.button(label, key=f"{status}-{r['id']}"):
                    store.resolve_pending(cx, r["id"], status)
                    rerun(cx)


def page_data(cx):
    settings = get_settings()
    st.write(f"**Base :** {describe(get_engine())}")
    if get_engine().dialect.name == "sqlite":
        st.warning("Base locale temporaire : les données seront perdues au redémarrage. Ajoutez DATABASE_URL (Neon) dans les Secrets.")
    if st.button("Charger les 14 événements connus et leurs 22 tâches"):
        st.success(importer.seed_events(cx, settings).line())
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
            st.success(rep.line())
            for w in rep.warnings[:30]:
                st.warning(w)
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
         "Automatisations": page_automation, "À valider": page_validate, "Données": page_data}
with st.sidebar:
    page = st.radio("Navigation", list(PAGES), label_visibility="collapsed")
    if st.button("Se déconnecter"):
        st.session_state.clear()
        st.rerun()

try:
    with get_engine().connect() as _cx:
        PAGES[page](_cx)
        _cx.commit()
except Exception as exc:  # noqa: BLE001
    st.error(f"Erreur : {exc}")
    st.caption("Si le message parle de connexion, vérifiez DATABASE_URL dans les Secrets.")
    if os.environ.get("PADEL_DEBUG"):
        raise
