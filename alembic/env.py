"""
Alembic environment.

Two jobs here: point Alembic at the same database the app uses (so there is one
source of truth for the URL, in core.config), and hand it our model metadata so
`--autogenerate` can diff the models against the live schema.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from core.config import get_settings
from core.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The URL lives in .env / core.config, not in alembic.ini, so the app and the
# migrations can never drift apart. Literal % must be doubled: alembic.ini is
# read by ConfigParser, which treats % as interpolation syntax (a generated
# Postgres password containing % would otherwise blow up here).
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))

# What `alembic revision --autogenerate` compares the database against.
target_metadata = Base.metadata

# SQLite has no meaningful ALTER TABLE: it cannot drop a column or alter a
# constraint in place. Batch mode makes Alembic emit a create-copy-drop-rename
# dance instead. Harmless on Postgres, essential on SQLite.
RENDER_AS_BATCH = True


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it -- useful for reviewing DDL."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=RENDER_AS_BATCH,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations for real."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=RENDER_AS_BATCH,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
