"""État RÉEL des connexions. Rien n'est jamais affiché « OK » sans avoir été vérifié par un appel réel.

États : ok (testé avec succès), erreur (test échoué), configure (secrets présents mais non testés ou connecteur absent
du code), absent (secrets manquants).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests
from sqlalchemy import text

from . import store


@dataclass
class Connection:
    key: str
    name: str
    state: str            # ok | erreur | configure | absent
    detail: str
    developed: bool       # le code du connecteur existe-t-il ?
    needs: str = ""       # ce qu'il faut fournir
    testable: bool = False


def check_database(engine) -> Connection:
    try:
        with engine.connect() as cx:
            cx.execute(text("SELECT 1"))
        local = engine.dialect.name == "sqlite"
        return Connection("database", "Base de données (Neon)", "configure" if local else "ok",
                          "Base locale temporaire : les données seront perdues au redémarrage." if local
                          else f"Connexion vérifiée ({engine.dialect.name}, {engine.url.host}).",
                          True, "DATABASE_URL" if local else "", True)
    except Exception as exc:  # noqa: BLE001
        return Connection("database", "Base de données (Neon)", "erreur", f"Échec : {exc}", True, "DATABASE_URL", True)


def check_typeform(test: bool = False) -> Connection:
    token = os.environ.get("TYPEFORM_TOKEN", "")
    if not token:
        return Connection("typeform", "Typeform", "absent", "Aucun jeton.", False, "TYPEFORM_TOKEN", False)
    if not test:
        return Connection("typeform", "Typeform", "configure",
                          "Jeton présent, non testé. L'import des réponses dans la base n'est pas encore branché.",
                          False, "", True)
    try:
        r = requests.get("https://api.typeform.com/me", headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if r.status_code == 200:
            return Connection("typeform", "Typeform", "ok", "Jeton valide. L'import des réponses dans la base "
                              "n'est pas encore branché.", False, "", True)
        return Connection("typeform", "Typeform", "erreur", f"Refusé par Typeform (HTTP {r.status_code}).", False,
                          "TYPEFORM_TOKEN", True)
    except requests.RequestException as exc:
        return Connection("typeform", "Typeform", "erreur", f"Injoignable : {exc}", False, "", True)


def check_microsoft() -> list[Connection]:
    have = all(os.environ.get(k) for k in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET"))
    detail = ("Secrets présents mais le connecteur n'est pas développé : rien n'est lu ni envoyé." if have
              else "Aucune application Microsoft 365 enregistrée, et connecteur non développé.")
    needs = "Enregistrement d'une application par l'administrateur Microsoft 365 (GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET)"
    state = "configure" if have else "absent"
    return [Connection("outlook", "Outlook (lecture et envoi des e-mails)", state, detail, False, needs),
            Connection("teams", "Teams (lecture et messages)", state, detail, False, needs)]


def check_claude() -> Connection:
    has = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return Connection("claude", "Claude (classification des réponses)", "configure" if has else "absent",
                      "Clé présente mais aucune fonctionnalité ne l'utilise encore." if has
                      else "Pas de clé, et fonctionnalité non développée.", False, "" if has else "ANTHROPIC_API_KEY")


def check_engine(cx, today: datetime | None = None) -> Connection:
    last = cx.execute(text("SELECT MAX(ts) FROM action_log WHERE action = 'engine_run'")).scalar()
    if isinstance(last, str):
        last = datetime.fromisoformat(last)
    now = today or store.now()
    if last is None:
        return Connection("engine", "Moteur d'automatisation (GitHub Actions)", "absent",
                          "Aucun passage enregistré. Ajoutez le secret DATABASE_URL dans GitHub puis lancez « padel-engine ».",
                          True, "Secret GitHub DATABASE_URL")
    age = now - last
    if age <= timedelta(minutes=45):
        return Connection("engine", "Moteur d'automatisation (GitHub Actions)", "ok",
                          f"Dernier passage il y a {int(age.total_seconds() // 60)} min.", True)
    return Connection("engine", "Moteur d'automatisation (GitHub Actions)", "erreur",
                      f"Dernier passage il y a {int(age.total_seconds() // 3600)} h : le moteur ne tourne plus "
                      "(vérifiez l'onglet Actions de GitHub).", True)


def all_connections(engine, cx, test_typeform: bool = False) -> list[Connection]:
    return [check_database(engine), check_engine(cx), check_typeform(test_typeform), *check_microsoft(), check_claude()]
