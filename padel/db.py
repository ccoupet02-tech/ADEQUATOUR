"""Base de données centrale (SQLAlchemy Core) : Neon/PostgreSQL en production, SQLite en local et en test.

La base de l'application est la source de référence : événements, personnes, inscriptions, tâches,
communications préparées, journal des actions et file « À valider ».
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import (Column, Date, DateTime, Float, ForeignKey, Integer, MetaData, String, Table, Text,
                        UniqueConstraint, create_engine)
from sqlalchemy.engine import Engine

metadata = MetaData()

events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(120), nullable=False, unique=True),
    Column("kind", String(60), default="Padel Tour"),
    Column("date", Date),
    Column("venue", String(200), default=""),
    Column("dr", String(120), default=""),            # responsable
    Column("status", String(30), default="preparation"),
    Column("capacity", Integer),
    Column("courts", Integer),
    Column("indoor_courts", Integer),
    Column("outdoor_courts", Integer),
    Column("price", Float),
    Column("contact_name", String(120), default=""),
    Column("contact_phone", String(60), default=""),
    Column("contact_email", String(200), default=""),
    Column("notes", Text, default=""),
    Column("created_at", DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)),
)

people = Table(
    "people", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pkey", String(300), nullable=False, unique=True),   # clé de rapprochement (nom + email/entreprise)
    Column("prenom", String(120), default=""),
    Column("nom", String(120), default=""),
    Column("email", String(200), default=""),
    Column("phone", String(60), default=""),
    Column("company", String(200), default=""),
    Column("position", String(200), default=""),
    Column("sector", String(120), default=""),
    Column("category", String(40), default="client"),
    Column("source", String(60), default=""),
    Column("created_at", DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)),
)

registrations = Table(
    "registrations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
    Column("person_id", Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False),
    Column("type", String(30), default=""),            # Tournoi / Initiation / Cocktail
    Column("team", String(120), default=""),
    Column("poule", String(30), default=""),
    Column("size", String(10), default=""),
    Column("level", String(30), default=""),
    Column("invite_status", String(20), default="a_inviter"),  # a_inviter, invite, accepte, refuse, erreur
    Column("invited_at", DateTime),
    Column("responded_at", DateTime),
    Column("source", String(60), default=""),
    UniqueConstraint("event_id", "person_id", name="uq_registration"),
)

tasks = Table(
    "tasks", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
    Column("key", String(300), nullable=False),         # nom normalisé sans suffixe « (1) »
    Column("name", String(300), nullable=False),
    Column("category", String(60), default=""),
    Column("offset_days", Integer),                     # échéance = date événement - offset
    Column("status", String(10), default="todo"),       # todo, doing, done
    Column("done_at", DateTime),
    Column("assignee", String(120), default=""),
    UniqueConstraint("event_id", "key", name="uq_task"),
)

staff = Table(
    "staff", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
    Column("nom", String(120), default=""),
    Column("prenom", String(120), default=""),
    Column("role", String(120), default=""),
    Column("shirt_size", String(10), default=""),
    Column("hotel", String(120), default=""),
    Column("transport", String(120), default=""),
    Column("repas", String(120), default=""),
)

communications = Table(
    "communications", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE")),
    Column("person_id", Integer, ForeignKey("people.id", ondelete="SET NULL")),
    Column("kind", String(40), nullable=False),         # invitation, relance, confirmation...
    Column("idem_key", String(300), nullable=False, unique=True),
    Column("to_addr", String(200), default=""),
    Column("subject", String(300), default=""),
    Column("body", Text, default=""),
    Column("status", String(20), default="preview"),    # preview, approved, sent, cancelled, error
    Column("created_at", DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)),
    Column("sent_at", DateTime),
)

action_log = Table(
    "action_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)),
    Column("actor", String(40), default="systeme"),     # systeme, utilisateur, import
    Column("action", String(60), nullable=False),
    Column("event_id", Integer),
    Column("detail", Text, default=""),
)

pending = Table(
    "pending", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("idem_key", String(300), nullable=False, unique=True),
    Column("event_id", Integer, ForeignKey("events.id", ondelete="CASCADE")),
    Column("kind", String(40), nullable=False),
    Column("title", String(300), nullable=False),
    Column("reason", Text, default=""),
    Column("status", String(20), default="open"),       # open, accepted, rejected, postponed
    Column("created_at", DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)),
    Column("resolved_at", DateTime),
)

TABLES = [events, people, registrations, tasks, staff, communications, action_log, pending]


def normalize_url(url: str) -> tuple[str, dict]:
    """URL Neon (postgresql://...?sslmode=require) -> URL SQLAlchemy pg8000 + arguments SSL.

    pg8000 est en Python pur (pas de dépendance binaire) et n'accepte pas les paramètres sslmode/channel_binding.
    """
    url = (url or "").strip()
    if url.startswith(("postgres://", "postgresql://")):
        url = "postgresql+pg8000://" + url.split("://", 1)[1]
    if url.startswith("postgresql+pg8000://"):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        needs_ssl = query.pop("sslmode", "") not in ("", "disable") or "neon.tech" in (parts.hostname or "")
        query.pop("channel_binding", None)
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
        return url, ({"ssl_context": True} if needs_ssl else {})
    return url, {}


def make_engine(url: str | None = None) -> Engine:
    url = url or os.environ.get("DATABASE_URL") or "sqlite:///data/padel.db"
    if url.startswith("sqlite:///") and url != "sqlite:///:memory:":
        os.makedirs(os.path.dirname(url.replace("sqlite:///", "")) or ".", exist_ok=True)
    final, connect_args = normalize_url(url)
    return create_engine(final, connect_args=connect_args, pool_pre_ping=True, future=True)


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)


def describe(engine: Engine) -> str:
    """Nom du moteur sans identifiants, pour l'affichage."""
    return f"{engine.dialect.name} · {engine.url.host or 'fichier local'}"
