"""Accès aux données : toutes les lectures/écritures de l'application passent par ici."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, insert, select, update

from .db import (action_log, app_settings, automations, communications, events, ignored_signups, pending, people, registrations, staff, sync_state,
                 tasks, workflow_runs)
from .util import base_task_name, norm

TASK_STATUS = {"todo": "À faire", "doing": "En cours", "done": "Terminée"}
INVITE_STATUS = {"a_inviter": "À inviter", "invite": "Invité (sans réponse)", "accepte": "Accepté",
                 "refuse": "Refusé", "erreur": "Erreur mail"}


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _empty(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


# ----------------------------------------------------------------------------- journal
def log(cx, action: str, event_id: int | None = None, detail: str = "", actor: str = "systeme") -> None:
    cx.execute(insert(action_log).values(ts=now(), actor=actor, action=action, event_id=event_id, detail=detail))


# ----------------------------------------------------------------------------- événements
def get_event(cx, ident) -> dict | None:
    if isinstance(ident, int):
        row = cx.execute(select(events).where(events.c.id == ident)).mappings().first()
        return dict(row) if row else None
    wanted = norm(ident)
    for row in cx.execute(select(events)).mappings():
        if norm(row["name"]) == wanted:
            return dict(row)
    return None


def list_events(cx) -> list[dict]:
    rows = cx.execute(select(events).order_by(events.c.date.is_(None), events.c.date)).mappings().all()
    return [dict(r) for r in rows]


def upsert_event(cx, name: str, **fields) -> tuple[int, bool]:
    """Crée l'événement ou complète ses champs vides. Renvoie (id, créé)."""
    name = name.strip().upper()
    existing = get_event(cx, name)
    clean = {k: v for k, v in fields.items() if not _empty(v)}
    if existing:
        patch = {k: v for k, v in clean.items() if _empty(existing.get(k))}
        if patch:
            cx.execute(update(events).where(events.c.id == existing["id"]).values(**patch))
        return existing["id"], False
    res = cx.execute(insert(events).values(name=name, **clean))
    return int(res.inserted_primary_key[0]), True


def update_event(cx, event_id: int, actor: str = "utilisateur", **fields) -> None:
    cx.execute(update(events).where(events.c.id == event_id).values(**fields))
    log(cx, "event_update", event_id, ", ".join(sorted(fields)), actor)


# ----------------------------------------------------------------------------- tâches
def task_key(name: str) -> str:
    return norm(base_task_name(name))


def deadline(event_date: date | None, offset: int | None) -> date | None:
    if event_date is None or offset is None:
        return None
    return event_date - timedelta(days=int(offset))


def classify(status: str, due: date | None, today: date, soon_days: int = 7) -> str:
    if status == "done":
        return "done"
    if due is None:
        return "undated"
    if due < today:
        return "late"
    if (due - today).days <= soon_days:
        return "soon"
    return "later"


def ensure_tasks(cx, event_id: int, templates) -> int:
    """Ajoute les tâches du référentiel absentes de l'événement (idempotent). Renvoie le nombre créé."""
    have = {r[0] for r in cx.execute(select(tasks.c.key).where(tasks.c.event_id == event_id))}
    created = 0
    for t in templates:
        key = task_key(t.name)
        if key in have:
            continue
        cx.execute(insert(tasks).values(event_id=event_id, key=key, name=t.name, category=t.category or "",
                                        offset_days=t.offset, status="todo"))
        have.add(key)
        created += 1
    if created:
        log(cx, "tasks_created", event_id, f"{created} tâche(s) du référentiel")
    return created


def list_tasks(cx, event_id: int | None = None) -> list[dict]:
    q = select(tasks, events.c.name.label("event_name"), events.c.date.label("event_date")).join(
        events, events.c.id == tasks.c.event_id)
    if event_id is not None:
        q = q.where(tasks.c.event_id == event_id)
    out = []
    for r in cx.execute(q).mappings():
        d = dict(r)
        d["deadline"] = deadline(d["event_date"], d["offset_days"])
        out.append(d)
    out.sort(key=lambda d: (d["event_date"] is None, d["event_date"] or date.max, d["deadline"] or date.max))
    return out


def set_task_status(cx, task_id: int, status: str, actor: str = "utilisateur") -> None:
    if status not in TASK_STATUS:
        raise ValueError(f"Statut inconnu : {status}")
    row = cx.execute(select(tasks).where(tasks.c.id == task_id)).mappings().first()
    if not row or row["status"] == status:
        return
    cx.execute(update(tasks).where(tasks.c.id == task_id).values(
        status=status, done_at=now() if status == "done" else None))
    log(cx, "task_status", row["event_id"], f"{row['name']} : {row['status']} -> {status}", actor)


# ----------------------------------------------------------------------------- personnes et inscriptions
def person_key(prenom: str, nom: str, email: str, company: str) -> str:
    return f"{norm(prenom)} {norm(nom)}|{(email or '').strip().lower() or norm(company)}"


def upsert_person(cx, overwrite: bool = False, **f) -> tuple[int, bool]:
    f = {k: (v.strip() if isinstance(v, str) else v) for k, v in f.items()}
    pkey = person_key(f.get("prenom", ""), f.get("nom", ""), f.get("email", ""), f.get("company", ""))
    row = cx.execute(select(people).where(people.c.pkey == pkey)).mappings().first()
    clean = {k: v for k, v in f.items() if not _empty(v)}
    if row:
        patch = {k: v for k, v in clean.items() if overwrite or _empty(row.get(k))}
        if patch:
            cx.execute(update(people).where(people.c.id == row["id"]).values(**patch))
        return row["id"], False
    res = cx.execute(insert(people).values(pkey=pkey, **clean))
    return int(res.inserted_primary_key[0]), True


def upsert_registration(cx, event_id: int, person_id: int, overwrite: bool = False, **f) -> tuple[int, bool]:
    row = cx.execute(select(registrations).where(and_(registrations.c.event_id == event_id,
                                                      registrations.c.person_id == person_id))).mappings().first()
    clean = {k: v for k, v in f.items() if not _empty(v)}
    if row:
        patch = {k: v for k, v in clean.items() if overwrite or _empty(row.get(k)) or k == "invite_status"}
        if patch:
            cx.execute(update(registrations).where(registrations.c.id == row["id"]).values(**patch))
        return row["id"], False
    res = cx.execute(insert(registrations).values(event_id=event_id, person_id=person_id, **clean))
    return int(res.inserted_primary_key[0]), True


def list_registrations(cx, event_id: int | None = None) -> list[dict]:
    q = select(registrations, people.c.prenom, people.c.nom, people.c.email, people.c.phone, people.c.company,
               people.c.position, events.c.name.label("event_name")).join(
        people, people.c.id == registrations.c.person_id).join(events, events.c.id == registrations.c.event_id)
    if event_id is not None:
        q = q.where(registrations.c.event_id == event_id)
    rows = [dict(r) for r in cx.execute(q).mappings()]
    rows.sort(key=lambda r: (norm(r["team"]), norm(r["nom"])))
    return rows


def participation(regs: list[dict], capacity: int | None = None) -> dict:
    """Chiffres clés des inscriptions : participants, équipes de 2, inscrits seuls, équipes à constituer, places."""
    tournoi = [r for r in regs if (r["type"] or "Tournoi").lower() != "cocktail"
               and r["invite_status"] != "refuse"]
    teams = {norm(r["team"]) for r in tournoi if r["team"]}
    solos = sum(1 for r in tournoi if not r["team"])
    out = {"participants": len(tournoi), "teams": len(teams), "solos": solos,
           "teams_total": len(teams) + (solos + 1) // 2, "odd": bool(solos % 2)}
    out["places_left"] = None if not capacity else max(capacity - len(tournoi), 0)
    return out


def reset_form_signups(cx, event_id: int, actor: str = "utilisateur") -> tuple[int, int]:
    """Supprime les inscriptions d'un événement venues du formulaire (source « formulaire ») et les personnes créées
    uniquement par elles. N'efface ni les saisies manuelles, ni les imports Notion. Retourne (inscriptions, personnes)."""
    ids = [r[0] for r in cx.execute(select(registrations.c.person_id).where(and_(
        registrations.c.event_id == event_id, registrations.c.source == "formulaire")))]
    n_reg = cx.execute(delete(registrations).where(and_(
        registrations.c.event_id == event_id, registrations.c.source == "formulaire"))).rowcount or 0
    n_people = 0
    for pid in set(ids):
        still = cx.execute(select(registrations.c.id).where(registrations.c.person_id == pid)).first()
        if not still:
            n_people += cx.execute(delete(people).where(and_(people.c.id == pid, people.c.source == "formulaire"))).rowcount or 0
    log(cx, "reset_signups", event_id, f"{n_reg} inscription(s) du formulaire et {n_people} personne(s) supprimée(s)", actor)
    return n_reg, n_people


def ignore_registration(cx, registration_id: int, reason: str = "", actor: str = "utilisateur") -> bool:
    """Ignore une inscription (test, doublon…) : elle quitte la liste et les prochaines synchronisations ne la recréent pas.
    La réponse reste dans Typeform ; « Rétablir » la fait revenir à la synchronisation suivante."""
    row = cx.execute(select(registrations.c.id, registrations.c.event_id, registrations.c.person_id, people.c.pkey,
                            people.c.prenom, people.c.nom, people.c.company, people.c.source).join(
        people, people.c.id == registrations.c.person_id).where(registrations.c.id == registration_id)).mappings().first()
    if not row:
        return False
    label = f"{row['prenom']} {row['nom']}".strip() or row["company"] or row["pkey"]
    if not cx.execute(select(ignored_signups.c.id).where(and_(ignored_signups.c.event_id == row["event_id"],
                                                              ignored_signups.c.pkey == row["pkey"]))).first():
        cx.execute(insert(ignored_signups).values(event_id=row["event_id"], pkey=row["pkey"], label=label[:200],
                                                  reason=reason[:200], created_at=now()))
    cx.execute(delete(registrations).where(registrations.c.id == registration_id))
    if row["source"] == "formulaire" and not cx.execute(
            select(registrations.c.id).where(registrations.c.person_id == row["person_id"])).first():
        cx.execute(delete(people).where(people.c.id == row["person_id"]))
    log(cx, "signup_ignored", row["event_id"], f"{label} ignoré(e)" + (f" ({reason})" if reason else ""), actor)
    return True


def list_ignored(cx, event_id: int) -> list[dict]:
    return [dict(r) for r in cx.execute(select(ignored_signups).where(
        ignored_signups.c.event_id == event_id).order_by(ignored_signups.c.id)).mappings()]


def unignore(cx, ignored_id: int, actor: str = "utilisateur") -> None:
    row = cx.execute(select(ignored_signups).where(ignored_signups.c.id == ignored_id)).mappings().first()
    if row:
        cx.execute(delete(ignored_signups).where(ignored_signups.c.id == ignored_id))
        log(cx, "signup_restored", row["event_id"], f"{row['label']} rétabli(e) (reviendra à la prochaine synchronisation)", actor)


def get_setting(cx, key: str, default: str = "") -> str:
    row = cx.execute(select(app_settings.c.value).where(app_settings.c.key == key)).first()
    return (row[0] or default) if row else default


def set_setting(cx, key: str, value: str, actor: str = "utilisateur") -> None:
    if cx.execute(select(app_settings.c.key).where(app_settings.c.key == key)).first():
        cx.execute(update(app_settings).where(app_settings.c.key == key).values(value=value))
    else:
        cx.execute(insert(app_settings).values(key=key, value=value))
    log(cx, "setting", None, f"{key} modifié", actor)


def set_poules(cx, event_id: int, mapping: dict[int, str], actor: str = "utilisateur") -> int:
    """Écrit la poule de chaque inscription (id d'inscription -> « A », « B »…). Retourne le nombre modifié."""
    n = 0
    for rid, poule in mapping.items():
        res = cx.execute(update(registrations).where(and_(registrations.c.id == int(rid),
                                                          registrations.c.event_id == event_id)).values(poule=poule))
        n += res.rowcount or 0
    log(cx, "poules", event_id, f"{n} inscription(s) affectée(s) à une poule", actor)
    return n


def set_invite_status(cx, registration_id: int, status: str, actor: str = "utilisateur") -> None:
    if status not in INVITE_STATUS:
        raise ValueError(f"Statut inconnu : {status}")
    row = cx.execute(select(registrations).where(registrations.c.id == registration_id)).mappings().first()
    if not row:
        return
    vals = {"invite_status": status}
    if status in ("accepte", "refuse"):
        vals["responded_at"] = now()
    if status == "invite" and row["invited_at"] is None:
        vals["invited_at"] = now()
    cx.execute(update(registrations).where(registrations.c.id == registration_id).values(**vals))
    log(cx, "invite_status", row["event_id"], f"inscription {registration_id} : {row['invite_status']} -> {status}", actor)
    if status in ("accepte", "refuse"):  # une réponse annule les relances encore en aperçu
        res = cx.execute(update(communications).where(and_(
            communications.c.event_id == row["event_id"], communications.c.person_id == row["person_id"],
            communications.c.kind == "relance", communications.c.status.in_(("preview", "approved")))
        ).values(status="cancelled"))
        if res.rowcount:
            log(cx, "relance_cancelled", row["event_id"], f"{res.rowcount} relance(s) annulée(s) : réponse reçue")


# ----------------------------------------------------------------------------- staff
STAFF_FIELDS = ("nom", "prenom", "role", "shirt_size", "hotel", "transport", "repas")  # + « source » géré en interne


def list_staff(cx, event_id: int) -> list[dict]:
    rows = cx.execute(select(staff).where(staff.c.event_id == event_id).order_by(staff.c.id)).mappings().all()
    return [dict(r) for r in rows]


def save_staff(cx, event_id: int, rows: list[dict], actor: str = "utilisateur") -> int:
    """Remplace la liste du staff de l'événement par celle de l'éditeur (lignes vides ignorées). Une ligne qui vient du
    Google Sheet garde son origine (la synchro continue de la mettre à jour)."""
    origin = {(norm(r["nom"]), norm(r["prenom"])): r.get("source") or "" for r in list_staff(cx, event_id)}
    clean = []
    for r in rows:
        item = {k: (str(r.get(k) or "").strip()) for k in STAFF_FIELDS}
        if any(item.values()):
            item["source"] = origin.get((norm(item["nom"]), norm(item["prenom"])), "")
            clean.append(item)
    cx.execute(delete(staff).where(staff.c.event_id == event_id))
    for item in clean:
        cx.execute(insert(staff).values(event_id=event_id, **item))
    log(cx, "staff_saved", event_id, f"{len(clean)} membre(s)", actor)
    return len(clean)


def staff_summary(rows: list[dict], roles: list[dict]) -> dict:
    """Chiffres du staff : inscrits, déjeuners, tailles de polo et répartition par rôle (avec l'effectif visé)."""
    names = {norm(r["name"]): r for r in roles}
    by_role = {r["name"]: 0 for r in roles}
    unassigned = other = lunch = 0
    sizes: dict[str, int] = {}
    for r in rows:
        key = norm(r.get("role"))
        if not key:
            unassigned += 1
        elif key in names:
            by_role[names[key]["name"]] += 1
        else:
            hit = next((n for n in names if n in key), None)   # « Field lead (préféré) » -> Field lead
            if hit:
                by_role[names[hit]["name"]] += 1
            else:
                other += 1
        if norm(r.get("repas")).startswith(("oui", "yes", "o")):
            lunch += 1
        if r.get("shirt_size"):
            sizes[r["shirt_size"]] = sizes.get(r["shirt_size"], 0) + 1
    return {"total": len(rows), "lunch": lunch, "sizes": sizes, "by_role": by_role,
            "unassigned": unassigned, "other": other,
            "targets": {r["name"]: r.get("target") for r in roles}}


# ----------------------------------------------------------------------------- communications et validations
def _insert_once(cx, table, key_col: str, values: dict) -> bool:
    """INSERT ... ON CONFLICT DO NOTHING sur la clé d'idempotence : atomique, sans erreur en cas de course."""
    if cx.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    stmt = dialect_insert(table).values(**values).on_conflict_do_nothing(index_elements=[key_col])
    return bool(cx.execute(stmt).rowcount)


def record_communication(cx, idem_key: str, kind: str, event_id: int | None, person_id: int | None,
                         to_addr: str, subject: str, body: str, status: str = "preview") -> bool:
    """Enregistre une communication une seule fois par clé d'idempotence. Renvoie True si elle est nouvelle."""
    return _insert_once(cx, communications, "idem_key", dict(
        idem_key=idem_key, kind=kind, event_id=event_id, person_id=person_id, to_addr=to_addr, subject=subject,
        body=body, status=status, created_at=now()))


def add_pending(cx, idem_key: str, kind: str, title: str, reason: str, event_id: int | None = None) -> bool:
    return _insert_once(cx, pending, "idem_key", dict(
        idem_key=idem_key, kind=kind, title=title, reason=reason, event_id=event_id, status="open",
        created_at=now()))


def resolve_pending(cx, pending_id: int, status: str, actor: str = "utilisateur") -> None:
    if status not in ("accepted", "rejected", "postponed"):
        raise ValueError(status)
    cx.execute(update(pending).where(pending.c.id == pending_id).values(
        status=status, resolved_at=now() if status != "postponed" else None))
    log(cx, "pending_" + status, None, f"validation {pending_id}", actor)


def mark_sent(cx, comm_id: int, actor: str = "utilisateur") -> None:
    """Enregistre qu'une communication a été envoyée (à la main, tant qu'aucun connecteur d'envoi n'existe)."""
    row = cx.execute(select(communications).where(communications.c.id == comm_id)).mappings().first()
    if not row or row["status"] == "sent":
        return
    cx.execute(update(communications).where(communications.c.id == comm_id).values(status="sent", sent_at=now()))
    if row["kind"] == "invitation" and row["person_id"]:
        reg = cx.execute(select(registrations).where(and_(
            registrations.c.event_id == row["event_id"], registrations.c.person_id == row["person_id"]))).mappings().first()
        if reg and reg["invite_status"] == "a_inviter":
            set_invite_status(cx, reg["id"], "invite", actor)
    log(cx, "communication_sent", row["event_id"], f"{row['kind']} -> {row['to_addr']}", actor)


def set_communication_status(cx, comm_id: int, status: str, actor: str = "utilisateur") -> None:
    if status not in ("preview", "approved", "cancelled"):
        raise ValueError(status)
    cx.execute(update(communications).where(communications.c.id == comm_id).values(status=status))
    log(cx, "communication_" + status, None, f"communication {comm_id}", actor)


# ----------------------------------------------------------------------------- indicateurs
def kpis(cx, today: date, soon_days: int = 7) -> dict:
    evs = list_events(cx)
    upcoming = [e for e in evs if e["date"] and e["date"] >= today]
    tl = [t for t in list_tasks(cx) if t["event_date"] and t["event_date"] >= today - timedelta(days=14)]
    cls = [classify(t["status"], t["deadline"], today, soon_days) for t in tl]
    regs = cx.execute(select(registrations.c.invite_status, func.count()).group_by(registrations.c.invite_status)).all()
    by_status = {s: n for s, n in regs}
    midnight = datetime.combine(today, datetime.min.time())
    return {
        "events_upcoming": len(upcoming),
        "tasks_late": cls.count("late"),
        "tasks_soon": cls.count("soon"),
        "tasks_done": cls.count("done"),
        "tasks_total": len(cls),
        "registrations": sum(by_status.values()),
        "accepted": by_status.get("accepte", 0),
        "pending_open": cx.execute(select(func.count()).select_from(pending).where(pending.c.status == "open")).scalar() or 0,
        "previews": cx.execute(select(func.count()).select_from(communications).where(
            communications.c.status == "preview")).scalar() or 0,
        "actions_today": cx.execute(select(func.count()).select_from(action_log).where(
            action_log.c.ts >= midnight)).scalar() or 0,
        "last_run": cx.execute(select(func.max(action_log.c.ts)).where(action_log.c.action == "engine_run")).scalar(),
    }


# ----------------------------------------------------------------------------- automatisations et exécutions
DEFAULT_AUTOMATIONS = [
    ("invitations", "Préparer les invitations dues (aperçu)"),
    ("relances", "Préparer les relances sans réponse (aperçu)"),
    ("typeform", "Récupérer les nouvelles inscriptions Typeform"),
    ("staff_sheet", "Relire le Google Sheet du staff"),
    ("staff_typeform", "Récupérer les inscriptions du formulaire Typeform du staff"),
]
MAX_ATTEMPTS = 3


def ensure_automations(cx) -> None:
    have = {r[0] for r in cx.execute(select(automations.c.key))}
    for key, label in DEFAULT_AUTOMATIONS:
        if key not in have:
            cx.execute(insert(automations).values(key=key, label=label, enabled=1, mode="preview"))


def list_automations(cx) -> list[dict]:
    ensure_automations(cx)
    return [dict(r) for r in cx.execute(select(automations).order_by(automations.c.key)).mappings()]


def is_enabled(cx, key: str) -> bool:
    row = cx.execute(select(automations.c.enabled).where(automations.c.key == key)).first()
    return True if row is None else bool(row[0])


def set_automation_enabled(cx, key: str, enabled: bool, actor: str = "utilisateur") -> None:
    ensure_automations(cx)
    cx.execute(update(automations).where(automations.c.key == key).values(enabled=1 if enabled else 0))
    log(cx, "automation_enabled" if enabled else "automation_disabled", None, key, actor)


def consecutive_failures(cx, workflow: str, event_id: int) -> int:
    rows = cx.execute(select(workflow_runs.c.status).where(and_(
        workflow_runs.c.workflow == workflow, workflow_runs.c.event_id == event_id,
        workflow_runs.c.status != "simulation")).order_by(workflow_runs.c.id.desc()).limit(MAX_ATTEMPTS + 2)).all()
    n = 0
    for (status,) in rows:
        if status != "erreur":
            break
        n += 1
    return n


def record_run(cx, workflow: str, event_id: int | None, status: str, prepared: int = 0, flagged: int = 0,
               error: str = "", detail: str = "", trigger: str = "planifie", started_at: datetime | None = None) -> int:
    attempt = 1
    if status == "erreur" and event_id is not None:
        attempt = consecutive_failures(cx, workflow, event_id) + 1
    res = cx.execute(insert(workflow_runs).values(
        workflow=workflow, event_id=event_id, trigger=trigger, status=status, started_at=started_at or now(),
        finished_at=now(), prepared=prepared, flagged=flagged, attempt=attempt, error=error[:2000], detail=detail))
    if status == "erreur" and attempt >= MAX_ATTEMPTS and event_id is not None:
        add_pending(cx, f"wf-fail:{workflow}:{event_id}", "automatisation_en_echec",
                    f"Automatisation « {workflow} » en échec répété ({attempt} fois)",
                    f"Dernière erreur : {error[:300]}", event_id)
    return int(res.inserted_primary_key[0])


def list_runs(cx, event_id: int | None = None, status: str | None = None, limit: int = 200) -> list[dict]:
    q = select(workflow_runs, events.c.name.label("event_name")).join(
        events, events.c.id == workflow_runs.c.event_id, isouter=True)
    if event_id is not None:
        q = q.where(workflow_runs.c.event_id == event_id)
    if status:
        q = q.where(workflow_runs.c.status == status)
    return [dict(r) for r in cx.execute(q.order_by(workflow_runs.c.id.desc()).limit(limit)).mappings()]


def latest_run_status(cx, workflow: str, event_id: int) -> str | None:
    row = cx.execute(select(workflow_runs.c.status).where(and_(
        workflow_runs.c.workflow == workflow, workflow_runs.c.event_id == event_id,
        workflow_runs.c.status != "simulation")).order_by(workflow_runs.c.id.desc()).limit(1)).first()
    return row[0] if row else None


def set_sync(cx, connector: str, status: str, error: str = "") -> None:
    row = cx.execute(select(sync_state).where(sync_state.c.connector == connector)).first()
    vals = {"status": status}
    if status == "ok":
        vals["last_ok_at"] = now()
    if status == "erreur":
        vals.update(last_error_at=now(), last_error=error[:1000])
    if row:
        cx.execute(update(sync_state).where(sync_state.c.connector == connector).values(**vals))
    else:
        cx.execute(insert(sync_state).values(connector=connector, **vals))


def get_sync(cx) -> dict[str, dict]:
    return {r["connector"]: dict(r) for r in cx.execute(select(sync_state)).mappings()}


# ----------------------------------------------------------------------------- indicateurs par événement
def event_kpis(cx, event: dict, today: date, soon_days: int = 7) -> dict:
    regs = list_registrations(cx, event["id"])
    tl = list_tasks(cx, event["id"])
    cls = [classify(t["status"], t["deadline"], today, soon_days) for t in tl]
    upcoming = sorted([t for t, c in zip(tl, cls) if c in ("soon", "later") and t["deadline"]], key=lambda t: t["deadline"])
    runs = list_runs(cx, event["id"], limit=500)
    failing = sorted({r["workflow"] for r in runs if r["status"] == "erreur"
                      and latest_run_status(cx, r["workflow"], event["id"]) == "erreur"})
    to_handle = cx.execute(select(func.count()).select_from(communications).where(and_(
        communications.c.event_id == event["id"], communications.c.status.in_(("preview", "approved"))))).scalar() or 0
    flagged = cx.execute(select(func.count()).select_from(pending).where(and_(
        pending.c.event_id == event["id"], pending.c.status.in_(("open", "postponed"))))).scalar() or 0
    n = len(regs)
    return {
        "registrations": n,
        "accepted": sum(r["invite_status"] == "accepte" for r in regs),
        "refused": sum(r["invite_status"] == "refuse" for r in regs),
        "no_answer": sum(r["invite_status"] == "invite" for r in regs),
        "to_invite": sum(r["invite_status"] == "a_inviter" for r in regs),
        "emails_to_handle": to_handle,
        "tasks_late": cls.count("late"),
        "tasks_soon": cls.count("soon"),
        "tasks_done": cls.count("done"),
        "tasks_total": len(tl),
        "progress": (cls.count("done") / len(tl)) if tl else 0.0,
        "next_actions": upcoming[:5],
        "failing": failing,
        "flagged": flagged,
    }
