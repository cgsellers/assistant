"""
The database role the application connects as.

Row-level security (tenancy step 4.3) binds every role except two: a
superuser, always, and a table's owner, unless the table FORCEs it. The
migrations run as the owner, which needs to create and alter tables. So the
API and the worker must connect as someone else, or the policies protect
nothing:

  * `ledgerline_app` -- may log in, read and write rows, and nothing more. Not
    a superuser, cannot bypass row-level security, owns no table, cannot
    create one, and may only read `alembic_version` (for `/ready`).

Run after the migrations, as the owner, on every deploy -- it is idempotent:

    APP_DB_PASSWORD=... uv run python -m core.dbroles

The password is the deployment's secret, so it is passed in and never kept in
a migration. Postgres only.
"""

from __future__ import annotations

import argparse
import logging
import os

from psycopg import sql
from sqlalchemy import Connection

APP_ROLE = "ledgerline_app"

log = logging.getLogger("dbroles")


def ensure_app_role(connection: Connection, password: str, *, role: str = APP_ROLE) -> None:
    """Create or update the application role, and grant it exactly what it needs."""
    if connection.dialect.name != "postgresql":
        raise RuntimeError("roles and row-level security are Postgres only")
    if not password:
        raise ValueError("the application role needs a password")

    raw = connection.connection.driver_connection  # psycopg, for safe DDL quoting
    name = sql.Identifier(role)
    with raw.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        verb = sql.SQL("ALTER") if cursor.fetchone() else sql.SQL("CREATE")
        # Every attribute stated, so a role someone loosened by hand is put
        # back: NOBYPASSRLS above all, or the policies stop applying to it.
        cursor.execute(sql.SQL(
            "{} ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE"
        ).format(verb, name, sql.Literal(password)))
        cursor.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(raw.info.dbname), name
        ))
        cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(name))
        cursor.execute(sql.SQL(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
        ).format(name))
        # Readiness reads the migration version; nothing else may touch it.
        cursor.execute(sql.SQL(
            "REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM {}"
        ).format(name))
        # Tables the owner creates in later migrations get the same rights
        # without anyone remembering to grant them.
        cursor.execute(sql.SQL(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
        ).format(name))
    # On the driver's connection, where the statements ran: SQLAlchemy never
    # saw a transaction begin, so its own commit() would do nothing, and the
    # role would be rolled back when the connection closed. (It was.)
    raw.commit()


def describe(connection: Connection, role: str = APP_ROLE) -> dict:
    """What a role is allowed: for the test that the app's role really is bound."""
    row = connection.exec_driver_sql(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %(role)s",
        {"role": role},
    ).one()
    owns = connection.exec_driver_sql(
        "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' "
        "AND tableowner = %(role)s",
        {"role": role},
    ).scalar()
    return {"superuser": row.rolsuper, "bypasses_rls": row.rolbypassrls, "owns_tables": owns}


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update the application's DB role")
    parser.add_argument(
        "--password-env", default="APP_DB_PASSWORD",
        help="the environment variable holding its password (default APP_DB_PASSWORD)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    password = os.environ.get(args.password_env, "")
    if not password:
        raise SystemExit(f"set {args.password_env} to the application role's password")

    from core.db import engine

    with engine.connect() as connection:
        ensure_app_role(connection, password)
    log.info("%s is ready: rows only, bound by row-level security", APP_ROLE)


if __name__ == "__main__":
    main()
