"""Tâches : génération depuis le référentiel, lecture, statuts, classement retard / à venir."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from .config import Settings, TaskTemplate
from .util import base_task_name, norm, norm_id

log = logging.getLogger("padel.tasks")


@dataclass
class Task:
    id: str
    name: str
    category: str | None
    offset: float | None
    status: str
    event_ids: list[str] = field(default_factory=list)

    def deadline(self, event_date: date | None) -> date | None:
        if event_date is None or self.offset is None:
            return None
        return compute_deadline(event_date, self.offset)

    @property
    def key(self) -> str:
        return norm(base_task_name(self.name))


def compute_deadline(event_date: date, offset: float) -> date:
    """Deadline = date de l'événement - offset jours (offset négatif = après l'événement)."""
    return event_date - timedelta(days=int(offset))


def fetch_tasks(notion, settings: Settings, event_id: str | None = None) -> list[Task]:
    schema = settings.schema("tasks")
    flt = schema.filter("event", "contains", event_id) if event_id else None
    tasks = []
    for page in notion.query_database(settings.tasks_db, flt):
        tasks.append(Task(
            id=page["id"],
            name=schema.read(page, "name") or "",
            category=schema.read(page, "category"),
            offset=schema.read(page, "offset"),
            status=schema.read(page, "status") or settings.status("not_started"),
            event_ids=schema.read(page, "event") or [],
        ))
    return tasks


def ensure_event_tasks(notion, settings: Settings, event, templates: list[TaskTemplate] | None = None,
                       existing: list[Task] | None = None) -> list[str]:
    """Crée les tâches manquantes de l'événement (idempotent). Retourne les noms créés."""
    templates = templates if templates is not None else settings.templates()
    existing = existing if existing is not None else fetch_tasks(notion, settings, event.id)
    have = {t.key for t in existing if t.name.strip()}
    schema = settings.schema("tasks")
    created = []
    for tpl in templates:
        if norm(tpl.name) in have:
            continue
        props = schema.build({
            "name": tpl.name,
            "category": tpl.category,
            "offset": tpl.offset,
            "status": settings.status("not_started"),
            "event": [event.id],
        })
        notion.create_page({"database_id": settings.tasks_db}, props)
        created.append(tpl.name)
    return created


def set_status(notion, settings: Settings, task: Task, status: str) -> bool:
    if task.status == status:
        return False
    notion.update_page(task.id, settings.schema("tasks").build({"status": status}))
    task.status = status
    return True


def mark_task(notion, settings: Settings, event, key: str, status: str,
              tasks: list[Task] | None = None) -> bool:
    """Passe à `status` la tâche du référentiel portant la clé `key` (ex: 'relance').
    Une tâche déjà « Done » n'est jamais rétrogradée."""
    tpl = next((t for t in settings.templates() if t.key == key), None)
    if tpl is None:
        raise KeyError(f"Aucune tâche du référentiel n'a la clé '{key}'")
    tasks = tasks if tasks is not None else fetch_tasks(notion, settings, event.id)
    task = next((t for t in tasks if t.key == norm(tpl.name)), None)
    if task is None:
        log.warning("Tâche '%s' introuvable pour %s", tpl.name, event.name)
        return False
    if task.status == settings.status("done") and status != settings.status("done"):
        return False
    return set_status(notion, settings, task, status)


def classify(tasks: list[Task], event_date: date | None, today: date, done_status: str = "Done",
             soon_days: int = 7) -> dict[str, list[tuple[Task, date | None]]]:
    """Range les tâches : done / late / soon / later / undated (triées par échéance)."""
    out = {"done": [], "late": [], "soon": [], "later": [], "undated": []}
    for t in tasks:
        if not t.name.strip():
            continue
        dl = t.deadline(event_date)
        if t.status == done_status:
            out["done"].append((t, dl))
        elif dl is None:
            out["undated"].append((t, dl))
        elif dl < today:
            out["late"].append((t, dl))
        elif dl <= today + timedelta(days=soon_days):
            out["soon"].append((t, dl))
        else:
            out["later"].append((t, dl))
    for k in ("late", "soon", "later"):
        out[k].sort(key=lambda x: x[1])
    return out


def same_event(a: str, b: str) -> bool:
    return norm_id(a) == norm_id(b)
