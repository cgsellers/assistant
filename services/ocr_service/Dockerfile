# Built from the repository root (see docker-compose.yml), so the PDF
# text-layer reader can be shared with the pipeline instead of copied. The
# root .dockerignore keeps .env and data/ out of the build context.
#
# Native arm64 -- no --platform flag, so this builds for whatever the host is.
FROM python:3.12-slim

# RapidOCR pulls in OpenCV for image decoding, which needs these shared
# libraries. The slim base image omits them to stay small, so we add just
# the two rather than switching to the full image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# Copy requirements alone first. Docker caches each step, and this one is the
# slow part -- as long as requirements.txt is unchanged, editing app.py below
# reuses the cached install instead of redownloading everything.
COPY services/ocr_service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The text-layer reader, shared with the pipeline so the two cannot drift
# apart. These three files and nothing else: the parser of untrusted uploads
# gets no other application code, and none of its configuration.
COPY ocr/__init__.py ocr/base.py ocr/pdftext.py ./ocr/
COPY services/ocr_service/app.py .

# Not root. Of every process in the system this is the one most likely to be
# handed a hostile file, so it gets the least: compose also drops every
# capability and mounts the filesystem read-only, and CI fails if either
# image runs as root.
RUN useradd --system --uid 10001 --no-create-home ocr
USER ocr

EXPOSE 8001
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
