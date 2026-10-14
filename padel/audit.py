"""Audit de la to-do : repère ce qui doit être nettoyé AVANT d'automatiser."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .config import Settings
from .tasks import Task
from .util import base_task_name, norm, norm_id


def audit_tasks(settings: Settings, tasks: list[Task], events: list) -> list[str]:
    tpl = {norm(t.name): t for t in settings.templates()}
    names = {norm_id(e.id): e.name for e in events}
    issues: list[str] = []
    empty = [t for t in tasks if not t.name.strip()]
    if empty:
        issues.append(f"{len(empty)} ligne(s) sans titre (à supprimer)")
    suffixed = [t for t in tasks if re.search(r"\(\d+\)\s*$", t.name or "")]
    if suffixed:
        issues.append(f"{len(suffixed)} tâche(s) avec suffixe de duplication « (1) », « (2) »… "
                      f"(ex: {suffixed[0].name!r}) - le pipeline les reconnaît mais renommez-les")
    orphans = [t for t in tasks if t.name.strip() and not t.event_ids]
    if orphans:
        issues.append(f"{len(orphans)} tâche(s) sans événement lié (ex: {orphans[0].name!r})")
    for t in tasks:
        ref = tpl.get(norm(base_task_name(t.name)))
        if t.name.strip() and ref is None:
            issues.append(f"Tâche hors référentiel : {t.name!r} (ajoutez-la à task_templates si récurrente)")
        elif ref and t.offset is not None and int(t.offset) != ref.offset:
            ev = ", ".join(names.get(norm_id(i), "?") for i in t.event_ids) or "?"
            issues.append(f"Offset différent du référentiel : {base_task_name(t.name)!r} = {int(t.offset)} "
                          f"pour {ev} (référentiel : {ref.offset})")
        elif ref and t.offset is None:
            issues.append(f"Offset vide : {base_task_name(t.name)!r}")
    per_event: dict[str, set] = defaultdict(set)
    dupes = Counter()
    for t in tasks:
        for i in t.event_ids:
            key = (norm_id(i), norm(base_task_name(t.name)))
            dupes[key] += 1
            per_event[norm_id(i)].add(norm(base_task_name(t.name)))
    for (eid, key), n in dupes.items():
        if n > 1 and key:
            issues.append(f"Doublon : {key!r} apparaît {n} fois pour {names.get(eid, '?')}")
    for e in events:
        missing = [t.name for t in settings.templates() if norm(t.name) not in per_event.get(norm_id(e.id), set())]
        if len(missing) == len(settings.templates()):
            issues.append(f"{e.name} : aucune tâche (lancez `sync-tasks --event {e.name} --apply`)")
        elif missing:
            issues.append(f"{e.name} : {len(missing)} tâche(s) du référentiel absente(s) (sync-tasks les crée)")
    return issues
