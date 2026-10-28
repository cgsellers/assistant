# The application image. The API, the worker and the one-off migrate step all
# run from it, with different commands -- see docker-compose.yml. One image
# means the code that migrated the database is exactly the code serving it.
#
# Two stages: dependencies are installed with uv in the first, and only the
# finished environment is copied into the second, so the runtime carries no
# installer -- the uv binary alone is 45 MB.

FROM python:3.12-slim AS dependencies

# uv from its own image, pinned, rather than `pip install uv`.
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app
# From the lockfile, so the image has exactly the versions the tests ran
# against. --no-install-project: Ledgerline is an application, not a package;
# its code is copied in below. --no-dev: no pytest, ruff or Pillow at runtime.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv

# Only what runs. Not tests/, evals/ (development tooling, and the corpus is
# private) or services/ (the OCR container is its own image). The root
# .dockerignore keeps .env and data/ out of the context altogether.
COPY alembic.ini ./
COPY alembic ./alembic
COPY analyst ./analyst
COPY api ./api
COPY core ./core
COPY demo ./demo
COPY doctypes ./doctypes
COPY llm ./llm
COPY ocr ./ocr
COPY panel ./panel
COPY worker ./worker

# Not root. The only thing it writes is the document store, mounted at
# STORAGE_DIR and owned by this user.
RUN useradd --system --uid 10001 --no-create-home ledgerline \
    && mkdir -p /data/files && chown ledgerline /data/files
USER ledgerline
ENV STORAGE_DIR=/data/files

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
