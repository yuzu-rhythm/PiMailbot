"""Bounded, path-safe local image attachment storage."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import uuid

from .storage import SqliteStore


class AttachmentError(ValueError):
    """Raised when an attachment cannot be safely stored."""


_SAFE_CHARACTER = re.compile(r"[^A-Za-z0-9._-]+")
_IMAGE_TYPES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
)


def _image_type(data: bytes, content_type: str | None) -> tuple[str, str]:
    for magic, detected_type, extension in _IMAGE_TYPES:
        if data.startswith(magic):
            if content_type and content_type.split(";", 1)[0].lower() not in {
                detected_type,
                "application/octet-stream",
            }:
                raise AttachmentError("attachment content type does not match image bytes")
            return detected_type, extension
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if content_type and content_type.split(";", 1)[0].lower() not in {
            "image/webp",
            "application/octet-stream",
        }:
            raise AttachmentError("attachment content type does not match image bytes")
        return "image/webp", ".webp"
    raise AttachmentError("only PNG, JPEG, GIF, and WEBP images are accepted")


def safe_attachment_stem(original_name: str) -> str:
    """Create a short ASCII stem that cannot introduce path separators."""

    if not isinstance(original_name, str):
        raise AttachmentError("attachment name must be text")
    name = unicodedata.normalize("NFKC", original_name).replace("\x00", "")
    name = Path(name).name
    stem = Path(name).stem
    stem = _SAFE_CHARACTER.sub("_", stem).strip("._-")
    if not stem:
        stem = "image"
    return stem[:48]


class AttachmentStore:
    """Store image bytes under a private directory and register their hash."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        database: SqliteStore,
        *,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.database = database
        self.max_bytes = max_bytes

    def save_image(
        self,
        original_name: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> dict[str, object]:
        if not isinstance(data, bytes):
            raise TypeError("image data must be bytes")
        if not data:
            raise AttachmentError("empty image attachment")
        if len(data) > self.max_bytes:
            raise AttachmentError(f"image exceeds the {self.max_bytes}-byte limit")
        detected_type, extension = _image_type(data, content_type)
        digest = hashlib.sha256(data).hexdigest()
        existing = self.database.find_image_by_hash(digest)
        if existing is not None:
            return existing

        stem = safe_attachment_stem(original_name)
        stored_name = f"{stem}-{digest[:16]}{extension}"
        destination = (self.root / stored_name).resolve()
        root = self.root.resolve()
        if destination.parent != root:
            raise AttachmentError("resolved attachment path escaped image directory")

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".mailbot-image-",
                suffix=".tmp",
                dir=root,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            # A same-content destination is harmless to replace because its
            # name is derived from this SHA-256.  The temporary file ensures a
            # crash cannot leave a partially written final image.
            os.replace(temporary_path, destination)
            temporary_path = None
            record = self.database.register_image(
                image_id=uuid.uuid4().hex,
                original_name=Path(original_name).name or "image",
                stored_name=stored_name,
                stored_path=str(destination),
                sha256=digest,
                size=len(data),
                content_type=detected_type,
            )
            if Path(str(record["stored_path"])).resolve() != destination:
                # Another writer won the hash race with a different stem.
                # Remove only this content's deterministic local copy.
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
            return record
        except OSError as exc:
            raise AttachmentError("could not write image safely") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
