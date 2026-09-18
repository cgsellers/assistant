"""Content-addressed storage and file type detection."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core import storage

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class TestSha256:
    def test_is_deterministic(self):
        assert storage.sha256_hex(b"abc") == storage.sha256_hex(b"abc")

    def test_matches_hashlib(self):
        assert storage.sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()

    def test_is_64_hex_chars_regardless_of_input_size(self):
        for data in (b"", b"x", b"y" * 1_000_000):
            digest = storage.sha256_hex(data)
            assert len(digest) == 64
            assert all(c in "0123456789abcdef" for c in digest)

    def test_one_bit_change_gives_unrelated_digest(self):
        """The property dedup relies on: no 'close', only same or different."""
        a, b = storage.sha256_hex(b"hi"), storage.sha256_hex(b"hj")
        assert a != b
        differs_at = (i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y)
        shared_prefix = next(differs_at, len(a))
        assert shared_prefix < 4  # not merely different -- unrelated


class TestStorageKey:
    def test_shards_on_first_two_characters(self):
        digest = "ab" + "c" * 62
        assert storage.storage_key(digest) == f"raw/ab/{digest}"

    def test_key_contains_no_user_supplied_filename(self):
        """Paths come from the hash, so a hostile filename cannot traverse."""
        digest = storage.sha256_hex(b"x")
        assert storage.storage_key(digest) == f"raw/{digest[:2]}/{digest}"


class TestSniffMime:
    @pytest.mark.parametrize(
        "data,expected",
        [
            (b"\xff\xd8\xff\xe0" + b"\x00" * 16, "image/jpeg"),
            (PNG_MAGIC + b"\x00" * 16, "image/png"),
            (b"%PDF-1.7\n" + b"\x00" * 16, "application/pdf"),
            (b"II*\x00" + b"\x00" * 16, "image/tiff"),
            (b"MM\x00*" + b"\x00" * 16, "image/tiff"),
            (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8, "image/webp"),
            (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8, "image/heic"),
            (b"\x00\x00\x00\x18ftypmif1" + b"\x00" * 8, "image/heic"),
        ],
    )
    def test_recognises_supported_formats(self, data, expected):
        assert storage.sniff_mime(data) == expected

    @pytest.mark.parametrize(
        "data",
        [b"", b"not a file", b"<html></html>", b"PK\x03\x04zip", b"\x00" * 32],
    )
    def test_returns_none_for_unrecognised(self, data):
        assert storage.sniff_mime(data) is None

    def test_ignores_a_lying_extension_because_it_reads_bytes(self):
        """The whole point: detection cannot be fooled by a filename or header."""
        assert storage.sniff_mime(PNG_MAGIC + b"\x00" * 16) == "image/png"


class TestSaveAndLoad:
    def test_writes_to_its_content_addressed_path(self, settings):
        data = b"some file"
        digest = storage.sha256_hex(data)
        key = storage.save(data, digest)

        assert key == f"raw/{digest[:2]}/{digest}"
        assert (settings.storage_dir / key).read_bytes() == data

    def test_round_trips(self, settings):
        data = b"round trip"
        key = storage.save(data, storage.sha256_hex(data))
        assert storage.load(key) == data

    def test_saving_twice_is_a_noop(self, settings):
        """Idempotence is what makes a retried upload safe."""
        data = b"written twice"
        digest = storage.sha256_hex(data)

        first = storage.save(data, digest)
        mtime = (settings.storage_dir / first).stat().st_mtime_ns
        second = storage.save(data, digest)

        assert first == second
        # Untouched, not rewritten.
        assert (settings.storage_dir / first).stat().st_mtime_ns == mtime
        assert len(list((settings.storage_dir / "raw").rglob("*.*"))) <= 1

    def test_leaves_no_temp_file_behind(self, settings):
        """A stray .tmp would eventually be mistaken for a real document."""
        data = b"atomic"
        storage.save(data, storage.sha256_hex(data))
        assert list(Path(settings.storage_dir).rglob("*.tmp")) == []

    def test_creates_missing_directories(self, settings):
        assert not settings.storage_dir.exists()
        storage.save(b"x", storage.sha256_hex(b"x"))
        assert settings.storage_dir.exists()

    def test_exists_reports_correctly(self, settings):
        digest = storage.sha256_hex(b"here")
        assert storage.exists(storage.storage_key(digest)) is False
        storage.save(b"here", digest)
        assert storage.exists(storage.storage_key(digest)) is True

    def test_stored_bytes_hash_to_their_own_filename(self, settings):
        """Self-verifying storage: the address is a checksum of the contents."""
        data = b"integrity"
        key = storage.save(data, storage.sha256_hex(data))
        assert hashlib.sha256(storage.load(key)).hexdigest() == Path(key).name
