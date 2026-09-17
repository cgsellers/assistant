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
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8001
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
