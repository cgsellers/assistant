"""
Photos the right way up, before any engine reads them.

A phone often stores a photo sideways and adds an EXIF tag saying how to turn
it. Browsers obey the tag; OCR engines disagree -- Tesseract's image library
ignores it, OpenCV (inside the PP-OCR container) applies it. Turning every
photo upright first means every engine reads the page a person sees, so the
boxes they return land where the portal draws them, and text reads the right
way round, which recognises better than text on its side.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps, UnidentifiedImageError

#: Formats Pillow can open and write back without loss of what OCR needs.
_TURNABLE = {"image/jpeg", "image/png", "image/webp", "image/tiff"}


def upright(data: bytes, mime_type: str) -> tuple[bytes, tuple[int, int] | None]:
    """
    The image turned as its EXIF says, and its size as it now stands. The bytes
    are returned unchanged when no turn is needed; the size is None when
    Pillow cannot read the format (HEIC), and the bytes then go on as they are.
    """
    if mime_type not in _TURNABLE:
        return data, None
    try:
        with Image.open(io.BytesIO(data)) as image:
            orientation = image.getexif().get(0x0112, 1)
            if orientation == 1:
                return data, image.size
            turned = ImageOps.exif_transpose(image)
            buffer = io.BytesIO()
            # PNG: lossless, so turning does not cost the OCR any detail.
            turned.save(buffer, format="PNG")
            return buffer.getvalue(), turned.size
    except (UnidentifiedImageError, OSError):
        return data, None
