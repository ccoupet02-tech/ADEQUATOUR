"""Gestion de la base : initialisation, jeu de départ et imports CSV.

    python -m padel.dbcli init
    python -m padel.dbcli seed
    python -m padel.dbcli import-events   evenements.csv
    python -m padel.dbcli import-tasks    taches.csv
    python -m padel.dbcli import-participants participants.csv [--event DIJON]
"""
from __future__ import annotations

import argparse

from . import importer
from .config import load_settings
from .db import describe, init_db, make_engine


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m padel.dbcli", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("seed")
    for name in ("import-events", "import-tasks", "import-participants"):
        sp = sub.add_parser(name)
        sp.add_argument("file")
        if name == "import-participants":
            sp.add_argument("--event", help="événement par défaut si le CSV n'a pas de colonne Tournoi")
    args = ap.parse_args(argv)
    settings, engine = load_settings(), make_engine()
    init_db(engine)
    print("Base :", describe(engine))
    with engine.begin() as cx:
        if args.cmd == "seed":
            print(importer.seed_events(cx, settings).line())
        elif args.cmd == "import-events":
            print(importer.import_events(cx, args.file).line())
        elif args.cmd == "import-tasks":
            print(importer.import_tasks(cx, args.file).line())
        elif args.cmd == "import-participants":
            rep = importer.import_participants(cx, args.file, args.event, settings)
            print(rep.line())
            for w in rep.warnings[:20]:
                print("  !", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
