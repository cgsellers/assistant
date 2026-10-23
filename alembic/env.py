"""
Alembic environment.

Two jobs here: point Alembic at the same database the app uses (so there is one
source of truth for the URL, in core.config), and hand it our model metadata so
`--autogenerate` can diff the models against the live schema.
"""

from logging.config import fileConfig

from sqlalchemy import Column, DateTime, TypeDecorator, engine_from_config, pool

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


def render_item(type_: str, obj: object, autogen_context) -> str | bool:
    """
    Render our TypeDecorators as the plain SQL type underneath them.

    Without this, autogenerate emits `core.models.Money(...)` into the
    migration, which makes a permanent migration file depend on application
    code -- rename or delete that class and old migrations stop importing.
    (It also emitted the reference without the matching import, which failed
    outright with NameError.)

    A TypeDecorator adds no DDL of its own: the column type is entirely its
    `impl`, and the Python-side conversion only matters at runtime. Emitting
    the impl is therefore both accurate and stable.

    Returning False falls back to Alembic's default rendering.
    """
    if type_ == "type" and isinstance(obj, TypeDecorator):
        impl = getattr(obj, "impl_instance", obj.impl)
        return f"sa.{impl!r}"
    return False


def compare_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
    """
    Compare our TypeDecorators by the column type they actually create.

    Left to the default, `alembic check` compared `UTCDateTime` itself with
    the database's TIMESTAMP and reported a difference on every Postgres
    timestamp, including the correct ones. Swapping in the impl and asking
    the default comparison again makes the check mean something.

    SQLite types carry no timezone, so a timestamp there matches either way.
    None means "no opinion": everything that is not ours compares as usual.
    """
    if not isinstance(metadata_type, TypeDecorator):
        return None
    impl = metadata_type.load_dialect_impl(context.dialect)
    if context.dialect.name == "sqlite" and isinstance(impl, DateTime):
        return False
    return context.impl.compare_type(inspected_column, Column(metadata_column.name, impl))


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it -- useful for reviewing DDL."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=RENDER_AS_BATCH,
        render_item=render_item,
        compare_type=compare_type,
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
            render_item=render_item,
            compare_type=compare_type,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
