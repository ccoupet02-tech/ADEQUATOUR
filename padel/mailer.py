"""Rendu des modèles de mail (Jinja2) et envoi : dry-run (défaut), SMTP ou Microsoft Graph."""
from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .config import PROJECT_DIR, Settings
from .util import slug

log = logging.getLogger("padel.mailer")


@dataclass
class Mail:
    to: list[str]
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)


def render(template: str, templates_dir: Path | None = None, **ctx) -> tuple[str, str]:
    """Modèle .j2 : 1re ligne « Subject: ... », ligne vide, puis le corps. Variable manquante => erreur."""
    env = Environment(loader=FileSystemLoader(str(templates_dir or PROJECT_DIR / "templates" / "mail")),
                      autoescape=False, undefined=StrictUndefined, keep_trailing_newline=True)
    text = env.get_template(f"{template}.j2").render(**ctx)
    head, _, body = text.partition("\n")
    if not head.lower().startswith("subject:"):
        raise ValueError(f"Le modèle {template}.j2 doit commencer par « Subject: ... »")
    return head.split(":", 1)[1].strip(), body.lstrip("\n")


def _eml(mail: Mail, sender: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = mail.subject, sender, ", ".join(mail.to)
    if mail.cc:
        msg["Cc"] = ", ".join(mail.cc)
    msg.set_content(mail.body)
    return msg


class DryRunBackend:
    real = False

    def __init__(self, out_dir: Path, sender: str = "pipeline@example.invalid"):
        self.out_dir, self.sender, self.count = Path(out_dir), sender, 0

    def send(self, mail: Mail) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.count += 1
        name = f"{self.count:03d}-{slug(mail.to[0] if mail.to else 'sans-destinataire')}.eml"
        (self.out_dir / name).write_bytes(bytes(_eml(mail, self.sender)))


class SmtpBackend:
    real = True

    def __init__(self, settings: Settings):
        e = settings.env
        self.host, self.port = settings.require_env("SMTP_HOST"), int(e.get("SMTP_PORT") or 587)
        self.user, self.password = e.get("SMTP_USER"), e.get("SMTP_PASSWORD")
        self.sender = e.get("SMTP_FROM") or self.user or ""
        self.name = settings.get("mail", "from_name", default="")

    def send(self, mail: Mail) -> None:
        msg = _eml(mail, f"{self.name} <{self.sender}>" if self.name else self.sender)
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)


class GraphBackend:
    """Microsoft Graph, flux « client credentials » : permission applicative Mail.Send + consentement admin."""
    real = True

    def __init__(self, settings: Settings):
        self.tenant, self.client = settings.require_env("GRAPH_TENANT_ID"), settings.require_env("GRAPH_CLIENT_ID")
        self.secret, self.sender = settings.require_env("GRAPH_CLIENT_SECRET"), settings.require_env("GRAPH_SENDER")
        self._token = None

    def _auth(self) -> str:
        if not self._token:
            r = requests.post(f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token", timeout=30,
                              data={"client_id": self.client, "client_secret": self.secret,
                                    "scope": "https://graph.microsoft.com/.default",
                                    "grant_type": "client_credentials"})
            r.raise_for_status()
            self._token = r.json()["access_token"]
        return self._token

    def send(self, mail: Mail) -> None:
        addr = lambda xs: [{"emailAddress": {"address": a}} for a in xs]  # noqa: E731
        payload = {"message": {"subject": mail.subject, "body": {"contentType": "Text", "content": mail.body},
                               "toRecipients": addr(mail.to), "ccRecipients": addr(mail.cc)},
                   "saveToSentItems": True}
        r = requests.post(f"https://graph.microsoft.com/v1.0/users/{self.sender}/sendMail", json=payload,
                          headers={"Authorization": f"Bearer {self._auth()}"}, timeout=30)
        r.raise_for_status()


def get_backend(settings: Settings, send: bool, dry_dir: Path):
    """Sans send=True on n'envoie JAMAIS : les mails sont écrits en .eml pour relecture."""
    if not send:
        return DryRunBackend(dry_dir)
    kind = (settings.env.get("MAIL_BACKEND") or "dry-run").lower()
    if kind == "smtp":
        return SmtpBackend(settings)
    if kind == "graph":
        return GraphBackend(settings)
    log.warning("--send demandé mais MAIL_BACKEND=%s : écriture en .eml uniquement", kind)
    return DryRunBackend(dry_dir)
