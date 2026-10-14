"""Génération des PDF (reportlab, polices standard : accents OK, pas d'emoji) :
planning général, feuilles de poule, feuilles d'arbitres, diplômes, étiquettes badges (Avery L7160)."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .participants import Participant
from .tournament import Plan
from .util import fr_date

_styles = getSampleStyleSheet()
CELL = ParagraphStyle("cell", parent=_styles["Normal"], fontSize=9, leading=11)
CELL_BIG = ParagraphStyle("cellbig", parent=_styles["Normal"], fontSize=12, leading=14)
HEAD = colors.HexColor("#1f3a5f")


def _p(text, style=CELL):
    return Paragraph(escape(str(text)), style)


def _table(data, col_widths, repeat=1, zebra=True, row_heights=None):
    t = Table(data, colWidths=col_widths, repeatRows=repeat, rowHeights=row_heights)
    style = [("BACKGROUND", (0, 0), (-1, 0), HEAD), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
             ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, 0), 9),
             ("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
    if zebra:
        style += [("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f7")])]
    t.setStyle(TableStyle(style))
    return t


def _doc(path, title, landscape_mode=False):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return SimpleDocTemplate(str(path), pagesize=landscape(A4) if landscape_mode else A4, title=title,
                             leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm)


def _hhmm(dt) -> str:
    return dt.strftime("%H:%M")


def planning_pdf(path, brand: str, event_name: str, event_date: date | None, plan: Plan) -> Path:
    doc = _doc(path, f"Planning {event_name}")
    story = [Paragraph(f"{escape(brand)} - {escape(event_name)}", _styles["Title"]),
             Paragraph(f"Planning général - {fr_date(event_date)} - fin estimée {_hhmm(plan.end)}",
                       _styles["Heading3"]), Spacer(1, 6)]
    rows = [["Heure", "Terrain", "Poule", "Équipe A", "Équipe B"]]
    for m in sorted(plan.matches, key=lambda x: (x.slot, x.court)):
        rows.append([_hhmm(m.start), str(m.court), m.poule, _p(m.team_a.name), _p(m.team_b.name)])
    if len(rows) == 1:
        story.append(Paragraph("Aucun match planifié.", _styles["Normal"]))
    else:
        story.append(_table(rows, [20 * mm, 18 * mm, 16 * mm, 63 * mm, 63 * mm]))
    doc.build(story)
    return Path(path)


def poules_pdf(path, brand: str, event_name: str, plan: Plan) -> Path:
    doc = _doc(path, f"Poules {event_name}")
    story = []
    by_poule: dict[str, list] = {}
    for m in plan.matches:
        by_poule.setdefault(m.poule, []).append(m)
    for i, poule in enumerate(plan.poules):
        if i:
            story.append(PageBreak())
        story += [Paragraph(f"{escape(brand)} - {escape(event_name)}", _styles["Title"]),
                  Paragraph(f"Poule {poule.name}", _styles["Heading1"])]
        teams = [["N°", "Équipe", "Joueurs", "Niveau"]]
        for n, t in enumerate(sorted(poule.teams, key=lambda t: -t.level), 1):
            teams.append([str(n), _p(t.name), _p(" / ".join(p.full_name for p in t.members)), f"{t.level:.1f}"])
        story += [_table(teams, [12 * mm, 55 * mm, 90 * mm, 18 * mm]), Spacer(1, 10)]
        games = [["Heure", "Terrain", "Équipe A", "Score A", "Score B", "Équipe B"]]
        for m in sorted(by_poule.get(poule.name, []), key=lambda x: x.slot):
            games.append([_hhmm(m.start), str(m.court), _p(m.team_a.name), "", "", _p(m.team_b.name)])
        story += [Paragraph("Matchs", _styles["Heading3"]),
                  _table(games, [18 * mm, 16 * mm, 55 * mm, 18 * mm, 18 * mm, 55 * mm],
                         row_heights=[None] + [10 * mm] * (len(games) - 1)), Spacer(1, 10)]
        rank = [["Équipe", "Victoires", "Défaites", "Points", "Diff.", "Rang"]]
        rank += [[_p(t.name), "", "", "", "", ""] for t in poule.teams]
        story += [Paragraph("Classement", _styles["Heading3"]),
                  _table(rank, [60 * mm, 22 * mm, 22 * mm, 22 * mm, 22 * mm, 18 * mm], zebra=False,
                         row_heights=[None] + [9 * mm] * len(poule.teams))]
    if not story:
        story = [Paragraph("Aucune poule générée.", _styles["Normal"])]
    doc.build(story)
    return Path(path)


def referee_pdf(path, brand: str, event_name: str, plan: Plan) -> Path:
    """Une page par terrain : feuille de score à faire remplir par l'arbitre."""
    doc = _doc(path, f"Arbitres {event_name}")
    courts = sorted({m.court for m in plan.matches})
    story = []
    for i, court in enumerate(courts):
        if i:
            story.append(PageBreak())
        story += [Paragraph(f"{escape(brand)} - {escape(event_name)}", _styles["Title"]),
                  Paragraph(f"Terrain {court} - feuille d'arbitre", _styles["Heading1"])]
        rows = [["Heure", "Poule", "Équipe A", "Score A", "Score B", "Équipe B", "Visa"]]
        for m in sorted((m for m in plan.matches if m.court == court), key=lambda x: x.slot):
            rows.append([_hhmm(m.start), m.poule, _p(m.team_a.name, CELL_BIG), "", "",
                         _p(m.team_b.name, CELL_BIG), ""])
        story.append(_table(rows, [16 * mm, 14 * mm, 44 * mm, 18 * mm, 18 * mm, 44 * mm, 20 * mm],
                            row_heights=[None] + [16 * mm] * (len(rows) - 1)))
    if not story:
        story = [Paragraph("Aucun match planifié.", _styles["Normal"])]
    doc.build(story)
    return Path(path)


def _fit(text: str, font: str, size: float, max_width: float, min_size: float = 8) -> float:
    while size > min_size and stringWidth(text, font, size) > max_width:
        size -= 0.5
    return size


def diplomas_pdf(path, brand: str, event_name: str, event_date: date | None,
                 participants: list[Participant]) -> Path:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    w, h = landscape(A4)
    c = canvas.Canvas(str(path), pagesize=(w, h))
    c.setTitle(f"Diplômes {event_name}")
    people = [p for p in participants if p.full_name]
    for p in people:
        c.setStrokeColor(HEAD)
        c.setLineWidth(4)
        c.rect(12 * mm, 12 * mm, w - 24 * mm, h - 24 * mm)
        c.setLineWidth(1)
        c.rect(16 * mm, 16 * mm, w - 32 * mm, h - 32 * mm)
        c.setFillColor(HEAD)
        c.setFont("Helvetica-Bold", 38)
        c.drawCentredString(w / 2, h - 55 * mm, "DIPLÔME")
        c.setFont("Helvetica", 16)
        c.drawCentredString(w / 2, h - 75 * mm, "décerné à")
        name = f"{p.prenom} {p.nom.upper()}".strip()
        c.setFont("Helvetica-Bold", _fit(name, "Helvetica-Bold", 34, w - 60 * mm))
        c.drawCentredString(w / 2, h - 100 * mm, name)
        c.setFillColor(colors.black)
        c.setFont("Helvetica", 16)
        c.drawCentredString(w / 2, h - 125 * mm, "pour sa participation au")
        line = f"{brand} - {event_name}"
        c.setFont("Helvetica-Bold", _fit(line, "Helvetica-Bold", 22, w - 60 * mm))
        c.drawCentredString(w / 2, h - 140 * mm, line)
        c.setFont("Helvetica", 13)
        c.drawCentredString(w / 2, h - 155 * mm, fr_date(event_date))
        c.line(w - 100 * mm, 30 * mm, w - 35 * mm, 30 * mm)
        c.setFont("Helvetica", 10)
        c.drawCentredString(w - 67.5 * mm, 25 * mm, "L'organisation")
        c.showPage()
    if not people:
        c.setFont("Helvetica", 14)
        c.drawString(30 * mm, h / 2, "Aucun participant.")
        c.showPage()
    c.save()
    return Path(path)


# Avery L7160 : 3 colonnes x 7 lignes, étiquette 63,5 x 38,1 mm
L_COLS, L_ROWS = 3, 7
L_W, L_H = 63.5 * mm, 38.1 * mm
L_LEFT, L_TOP, L_PITCH_X = 7.2 * mm, 15.15 * mm, 66.0 * mm


def labels_pdf(path, brand: str, event_name: str, participants: list[Participant]) -> Path:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    w, h = A4
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Étiquettes {event_name}")
    per_page = L_COLS * L_ROWS
    people = sorted(participants, key=lambda p: (p.nom.lower(), p.prenom.lower()))
    for i, p in enumerate(people):
        if i and i % per_page == 0:
            c.showPage()
        col, row = (i % per_page) % L_COLS, (i % per_page) // L_COLS
        x = L_LEFT + col * L_PITCH_X
        y = h - L_TOP - (row + 1) * L_H
        pad, maxw = 3 * mm, L_W - 6 * mm
        name = f"{p.prenom} {p.nom.upper()}".strip() or p.email
        c.setFillColor(HEAD)
        c.setFont("Helvetica-Bold", _fit(name, "Helvetica-Bold", 15, maxw))
        c.drawString(x + pad, y + L_H - 12 * mm, name)
        c.setFillColor(colors.black)
        if p.company:
            c.setFont("Helvetica", _fit(p.company, "Helvetica", 11, maxw))
            c.drawString(x + pad, y + L_H - 19 * mm, p.company)
        poule = (p.poule if p.poule.lower().startswith("poule") else f"Poule {p.poule}") if p.poule else ""
        detail = " - ".join(v for v in (p.type or "Tournoi", poule) if v)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(x + pad, y + L_H - 26 * mm, detail)
        c.setFont("Helvetica", 7)
        c.setFillColor(colors.grey)
        c.drawString(x + pad, y + 3 * mm, f"{brand} - {event_name}"[:60])
    if not people:
        c.setFont("Helvetica", 12)
        c.drawString(20 * mm, h / 2, "Aucun participant.")
    c.showPage()
    c.save()
    return Path(path)
