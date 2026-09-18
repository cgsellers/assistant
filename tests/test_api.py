"""HTTP contract: upload, dedup, validation, and reading results back."""

from __future__ import annotations

from base64 import b64encode

from core.config import get_settings
from core.models import OcrResult

from .conftest import TINY_PNG

MINIMAL_PDF = b"%PDF-1.4\n" + b"\x00" * 64


def _upload(client, data=TINY_PNG, name="receipt.png", content_type="image/png"):
    return client.post("/documents", files={"file": (name, data, content_type)})


class TestHealth:
    def test_reports_ok(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "db": "ok"}


class TestUpload:
    def test_accepts_a_png(self, client):
        response = _upload(client)
        assert response.status_code == 200

        body = response.json()
        assert body["mime_type"] == "image/png"
        assert body["size_bytes"] == len(TINY_PNG)
        assert body["original_filename"] == "receipt.png"
        assert body["status"] == "uploaded"
        assert body["duplicate"] is False
        assert len(body["sha256"]) == 64

    def test_accepts_a_pdf(self, client):
        response = _upload(client, MINIMAL_PDF, "invoice.pdf", "application/pdf")
        assert response.status_code == 200
        assert response.json()["mime_type"] == "application/pdf"

    def test_detects_the_type_from_bytes_not_the_header(self, client):
        """A client claiming PDF while sending PNG gets recorded as PNG."""
        response = _upload(client, TINY_PNG, "lies.pdf", "application/pdf")
        assert response.status_code == 200
        assert response.json()["mime_type"] == "image/png"

    def test_rejects_unrecognised_bytes(self, client):
        response = _upload(client, b"definitely not an image" * 10)
        assert response.status_code == 415
        assert "Unrecognised" in response.json()["detail"]

    def test_rejects_an_empty_file(self, client):
        response = _upload(client, b"")
        assert response.status_code == 400
        assert response.json()["detail"] == "File is empty"

    def test_rejects_a_file_over_the_size_cap(self, client, monkeypatch):
        monkeypatch.setenv("MAX_UPLOAD_BYTES", "128")
        get_settings.cache_clear()

        response = _upload(client, TINY_PNG + b"\x00" * 500)
        assert response.status_code == 413
        assert "limit is" in response.json()["detail"]

    def test_rejects_a_disallowed_but_recognised_type(self, client, monkeypatch):
        """TIFF is detectable; here it is removed from the allowlist."""
        monkeypatch.setenv("ALLOWED_MIME_TYPES", '["image/png"]')
        get_settings.cache_clear()

        response = _upload(client, b"II*\x00" + b"\x00" * 64, "scan.tiff", "image/tiff")
        assert response.status_code == 415
        assert "not accepted" in response.json()["detail"]


class TestUploadDeduplication:
    def test_reuploading_returns_the_original_flagged_duplicate(self, client):
        first = _upload(client).json()
        second = _upload(client, name="different-name.png").json()

        assert second["id"] == first["id"]
        assert second["duplicate"] is True
        assert first["duplicate"] is False

    def test_uploaded_at_does_not_move_on_a_duplicate(self, client):
        """The tell that you were handed the original record, not a new one."""
        first = _upload(client).json()
        second = _upload(client).json()
        assert second["uploaded_at"] == first["uploaded_at"]

    def test_different_bytes_create_a_second_document(self, client):
        first = _upload(client, TINY_PNG).json()
        second = _upload(client, TINY_PNG + b"x").json()
        assert second["id"] != first["id"]
        assert second["duplicate"] is False


class TestBase64Upload:
    def test_accepts_base64_json(self, client):
        response = client.post(
            "/documents/base64",
            json={"content_b64": b64encode(TINY_PNG).decode(), "filename": "b64.png"},
        )
        assert response.status_code == 200
        assert response.json()["original_filename"] == "b64.png"
        assert response.json()["mime_type"] == "image/png"

    def test_dedupes_against_a_multipart_upload_of_the_same_bytes(self, client):
        """Both routes share one identity: the bytes."""
        first = _upload(client).json()
        second = client.post(
            "/documents/base64", json={"content_b64": b64encode(TINY_PNG).decode()}
        ).json()

        assert second["id"] == first["id"]
        assert second["duplicate"] is True

    def test_rejects_invalid_base64(self, client):
        response = client.post("/documents/base64", json={"content_b64": "!!!not b64!!!"})
        assert response.status_code == 400
        assert "not valid base64" in response.json()["detail"]

    def test_filename_is_optional(self, client):
        response = client.post(
            "/documents/base64", json={"content_b64": b64encode(TINY_PNG).decode()}
        )
        assert response.status_code == 200
        assert response.json()["original_filename"] is None


class TestGetDocument:
    def test_returns_status_and_audit_trail(self, client):
        document_id = _upload(client).json()["id"]
        body = client.get(f"/documents/{document_id}").json()

        assert body["id"] == document_id
        assert body["status"] == "uploaded"
        assert len(body["events"]) == 1
        assert body["events"][0]["from_status"] is None
        assert body["events"][0]["to_status"] == "uploaded"

    def test_404_for_an_unknown_id(self, client):
        response = client.get("/documents/not-a-real-id")
        assert response.status_code == 404
        assert response.json()["detail"] == "No such document"


class TestGetOcr:
    def test_404_before_ocr_has_run_naming_the_status(self, client):
        """Distinct from 'no such document' -- the caller must run the worker."""
        document_id = _upload(client).json()["id"]
        response = client.get(f"/documents/{document_id}/ocr")

        assert response.status_code == 404
        assert "No OCR result yet" in response.json()["detail"]
        assert "uploaded" in response.json()["detail"]

    def test_404_for_an_unknown_document(self, client):
        response = client.get("/documents/nope/ocr")
        assert response.status_code == 404
        assert response.json()["detail"] == "No such document"

    def test_returns_the_text_once_ocr_has_run(self, client, session):
        document_id = _upload(client).json()["id"]
        session.add(
            OcrResult(
                document_id=document_id,
                engine="fake",
                engine_version="1.0",
                full_text="CAFE LUNA\nTOTAL 9.35",
                layout_blocks=[
                    {"text": "TOTAL 9.35", "bbox": [1, 2, 3, 4], "confidence": 0.99, "page": 1}
                ],
                mean_confidence=0.99,
            )
        )
        session.commit()

        body = client.get(f"/documents/{document_id}/ocr").json()
        assert body["engine"] == "fake"
        assert body["full_text"] == "CAFE LUNA\nTOTAL 9.35"
        assert body["mean_confidence"] == 0.99
        assert body["block_count"] == 1
        assert body["layout_blocks"] is None  # omitted unless asked for

    def test_include_blocks_returns_the_boxes(self, client, session):
        document_id = _upload(client).json()["id"]
        session.add(
            OcrResult(
                document_id=document_id,
                engine="fake",
                full_text="x",
                layout_blocks=[{"text": "x", "bbox": [1, 2, 3, 4], "confidence": 0.5, "page": 1}],
                mean_confidence=0.5,
            )
        )
        session.commit()

        body = client.get(
            f"/documents/{document_id}/ocr", params={"include_blocks": True}
        ).json()
        assert body["layout_blocks"][0]["bbox"] == [1, 2, 3, 4]


class TestOpenApi:
    def test_all_endpoints_are_documented(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        assert set(paths) == {
            "/documents",
            "/documents/base64",
            "/documents/{document_id}",
            "/documents/{document_id}/ocr",
            "/health",
        }
