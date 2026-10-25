"""
Organisations: who owns what (DESIGN §9.3).

"Ledgerline demo" is the team's own organisation. Every document that existed
before tenancy was moved into it, and the team's uploads and keys land there
by default. It is an ordinary organisation in every other respect -- the
team's *users* see across organisations because they are staff, not because
of where they belong.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import Organisation

DEMO_SLUG = "ledgerline-demo"
DEMO_NAME = "Ledgerline demo"

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,58}[a-z0-9]$")


def demo_organisation(session: Session) -> Organisation:
    """The team's organisation, created on first use (the migration also creates it)."""
    found = by_slug(session, DEMO_SLUG)
    if found is not None:
        return found
    return create(session, DEMO_NAME, DEMO_SLUG)


def by_slug(session: Session, slug: str) -> Organisation | None:
    return session.scalar(select(Organisation).where(Organisation.slug == slug))


def create(session: Session, name: str, slug: str) -> Organisation:
    if not _SLUG.match(slug):
        raise ValueError("a slug is 2-60 lowercase letters, digits and hyphens")
    if by_slug(session, slug) is not None:
        raise ValueError(f"an organisation called {slug!r} already exists")
    organisation = Organisation(name=name.strip(), slug=slug)
    session.add(organisation)
    session.commit()
    return organisation


def all_organisations(session: Session) -> list[Organisation]:
    return list(session.scalars(select(Organisation).order_by(Organisation.name)))


def main() -> None:
    import argparse
    import logging

    parser = argparse.ArgumentParser(description="Manage organisations")
    sub = parser.add_subparsers(dest="command", required=True)
    made = sub.add_parser("create", help="add a client organisation")
    made.add_argument("name", help='its name, e.g. "Acme Ltd"')
    made.add_argument("slug", help="short and stable, e.g. acme")
    sub.add_parser("list", help="show organisations")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger("tenancy")

    from core.db import SessionLocal

    with SessionLocal() as session:
        if args.command == "create":
            try:
                organisation = create(session, args.name, args.slug)
            except ValueError as exc:
                raise SystemExit(str(exc)) from None
            log.info("created %s (%s)", organisation.name, organisation.slug)
            log.info("its users and keys: --client %s", organisation.slug)
            return
        for organisation in all_organisations(session):
            log.info("%-24s %s", organisation.slug, organisation.name)


if __name__ == "__main__":
    main()
