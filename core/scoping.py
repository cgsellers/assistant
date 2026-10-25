"""
Whose documents a database session may see -- enforced in one place.

Tenancy step 4.2 (DESIGN §9.3). Every session carries a `Scope`: one
organisation, or every organisation for the Ledgerline team and the system's
own processes. A hook on every ORM statement adds the organisation filter to
each table that holds client data -- documents and everything made from them
-- wherever that table appears: the main query, a join, a subquery, a
relationship load, a bulk UPDATE or DELETE. No route adds the filter, so no
route can forget it.

**It fails closed.** A statement that touches client data from a session with
no scope set raises `Unscoped` instead of returning everything. Code that
forgets to say whose data it is working with crashes in a test, rather than
leaking in production.

What it does not cover, and why that is acceptable:

  * **Raw SQL** (`text()`). Nothing in the application reads client data that
    way; the readiness check and the migrations do not touch it.
  * **Objects already in a session's identity map.** `session.get()` returns
    one without a query. A session's scope is set once, before anything is
    loaded, and never changed, so everything in its map was loaded under it.
  * **Inserts.** Nothing is filtered on the way in; ownership on write is
    4.1's job (the caller's organisation, and the before_flush hook).

The database enforces the same rule underneath, from step 4.3.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Table, event
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria
from sqlalchemy.sql import visitors

from core.models import Document, DocumentEvent, Extraction, Job, OcrResult, Transaction

#: Every table holding a client's data. Each has organisation_id (4.1).
CLIENT_DATA = (Document, OcrResult, DocumentEvent, Job, Extraction, Transaction)

_KEY = "ledgerline.scope"


class Unscoped(RuntimeError):
    """Client data was queried from a session nobody said the owner of."""


@dataclass(frozen=True)
class Scope:
    """Whose client data a session sees. Build with `of()` or `everyone()`."""

    #: None means every organisation.
    organisation_id: str | None
    #: Who asked for it, for the error message and the log: "key:erp", "worker".
    reason: str

    @classmethod
    def of(cls, organisation_id: str, reason: str) -> Scope:
        if not organisation_id:
            raise ValueError("an organisation scope needs an organisation")
        return cls(organisation_id, reason)

    @classmethod
    def everyone(cls, reason: str) -> Scope:
        """
        Every organisation. For the Ledgerline team's staff and the system's
        own processes -- the worker, the operator commands -- and nobody else.
        Never for a key: keys end up inside client systems.
        """
        return cls(None, reason)


def set_scope(session: Session, scope: Scope) -> None:
    """
    Fix a session's scope. Once, before anything is loaded: changing it later
    would leave objects from the old scope in the identity map, where
    `session.get()` returns them without asking the database.
    """
    current = session.info.get(_KEY)
    if current is not None and current != scope:
        raise RuntimeError(
            f"this session is already scoped to {current.reason}; open a new one"
        )
    session.info[_KEY] = scope


def get_scope(session: Session) -> Scope | None:
    return session.info.get(_KEY)


@event.listens_for(Session, "do_orm_execute")
def _scope_every_statement(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    tables = _client_tables_in(state.statement)
    if not tables:
        return

    scope = get_scope(state.session)
    if scope is None:
        raise Unscoped(
            f"queried {', '.join(sorted(t.name for t in tables))} from a session with no "
            "scope; call core.scoping.set_scope first -- Scope.of(organisation) for a "
            "client, Scope.everyone(...) only for staff and the system's own processes"
        )
    if scope.organisation_id is None:
        return

    # SQLAlchemy's own mechanism for exactly this: the criteria reach every
    # place the table appears -- the main FROM, joins (in the ON clause, so an
    # outer join keeps rows with no match), subqueries, eager loads, and bulk
    # UPDATE and DELETE. tests/test_scoping.py pins each of those shapes.
    organisation_id = scope.organisation_id
    state.statement = state.statement.options(
        *(
            with_loader_criteria(
                model,
                lambda cls: cls.organisation_id == organisation_id,
                include_aliases=True,
            )
            for model in CLIENT_DATA
        )
    )


_CLIENT_TABLES = frozenset(model.__table__ for model in CLIENT_DATA)


def _client_tables_in(statement) -> set:
    """
    Every client-data table anywhere in a statement -- not only the entities
    it selects. The first version of this hook looked only at those, so
    `select(func.count()).select_from(Document)`, which selects no entity,
    skipped the filter *and* the unscoped check and counted every client's
    documents. Found by probing that shape; tests/test_scoping.py keeps it.
    """
    found = set()
    for element in visitors.iterate(statement):
        if isinstance(element, Table) and element in _CLIENT_TABLES:
            found.add(element)
    return found
