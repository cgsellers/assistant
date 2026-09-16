"""
Content-addressed file storage.

A file's SHA-256 is its address: identical bytes always land on the same path,
so writing twice is a no-op rather than a duplicate. That property is what makes
upload idempotent -- a client retrying a failed request cannot create a second
copy.

Local filesystem for now. Swapping in S3 later means reimplementing save/load
against the same two-function surface.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.config import get_settings


def sha256_hex(data: bytes) -> str:
    """The dedup key. Hex rather than bytes so it can live in a VARCHAR column."""
    return hashlib.sha256(data).hexdigest()


def storage_key(digest: str) -> str:
    """
    Path for a digest, relative to STORAGE_DIR.

    Sharded on the first two hex characters: 256 subdirectories instead of one
    flat folder with a million entries, which most filesystems handle badly.
    No file extension -- the mime type lives in the database, and deriving a
    path from a user-supplied filename is how you get path traversal.
    """
    return f"raw/{digest[:2]}/{digest}"


def _absolute(key: str) -> Path:
    return get_settings().storage_dir / key


def save(data: bytes, digest: str) -> str:
    """
    Write bytes to their content-addressed location. Returns the storage key.

    Idempotent: if the file is already there, the bytes are by definition
    identical, so there is nothing to do.
    """
    key = storage_key(digest)
    path = _absolute(key)

    if path.exists():
        return key

    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp name and rename. rename() is atomic on POSIX, so a crash
    # mid-write leaves a stray .tmp rather than a truncated file that would
    # later be trusted as a complete document.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return key


def load(key: str) -> bytes:
    """Read a stored file back. The OCR worker uses this."""
    return _absolute(key).read_bytes()


def exists(key: str) -> bool:
    return _absolute(key).exists()


# Magic-byte signatures. The browser's declared Content-Type is client-supplied
# and trivially forged, so the mime type we store is the one we detect here.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)


def sniff_mime(data: bytes) -> str | None:
    """
    Identify a file from its leading bytes. Returns None if unrecognised.

    Container formats (WebP, HEIC) carry their brand at a fixed offset rather
    than position zero, so they are checked separately.
    """
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime

    # RIFF....WEBP -- 4-byte tag, 4-byte length, then the form type.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    # ISO base media: size, then 'ftyp', then a brand. HEIC photos from iPhones
    # land here, which matters because that is the default camera format.
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"mif1"):
        return "image/heic"

    return None
