from __future__ import annotations

import json
import os
import shutil
import tempfile
from hashlib import sha256
from pathlib import Path

from .models import ThreadPost


def source_fingerprint(post: ThreadPost) -> str:
    source = {
        "id": post.id,
        "title": post.source_title,
        "text": post.text,
        "timestamp": post.timestamp.isoformat(),
    }
    return sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def cover_queue_entry(post: ThreadPost) -> dict[str, str]:
    return {
        "id": post.id,
        "fingerprint": source_fingerprint(post),
        "title": post.title,
        "text": post.text,
    }


def write_private_queue(path: Path, entries: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".cover-queue-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump({"items": entries}, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CoverStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _base(self, item_id: str, fingerprint: str) -> Path:
        if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("Invalid cover fingerprint.")
        return self.directory / sha256(item_id.encode()).hexdigest() / fingerprint

    def install(self, item_id: str, fingerprint: str, image: Path) -> Path:
        suffix = _image_suffix(image)
        base = self._base(item_id, fingerprint)
        base.parent.mkdir(parents=True, exist_ok=True)
        destination = base.with_suffix(suffix)
        fd, temporary = tempfile.mkstemp(prefix=".cover-", dir=base.parent)
        try:
            with os.fdopen(fd, "wb") as target, image.open("rb") as source:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        digest = _file_sha256(destination)
        write_private_queue(
            base.with_suffix(".json"),
            [{"id": item_id, "fingerprint": fingerprint, "file": destination.name, "sha256": digest}],
        )
        return destination

    def resolve(self, post: ThreadPost) -> Path:
        base = self._base(post.id, source_fingerprint(post))
        manifest = base.with_suffix(".json")
        if not manifest.is_file():
            raise RuntimeError(f"Missing generated cover for source item {post.id}.")
        try:
            entries = json.loads(manifest.read_text(encoding="utf-8"))["items"]
            record = entries[0]
            if len(entries) != 1 or record["id"] != post.id or record["fingerprint"] != base.name:
                raise ValueError("cover metadata mismatch")
            image = base.parent / record["file"]
            if image not in (base.with_suffix(".png"), base.with_suffix(".jpg")):
                raise ValueError("cover filename mismatch")
            if _image_suffix(image) != image.suffix or _file_sha256(image) != record["sha256"]:
                raise ValueError("cover image mismatch")
        except (OSError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Invalid generated cover for source item {post.id}.") from error
        return image


def _image_suffix(path: Path) -> str:
    with path.open("rb") as file:
        first = file.read(24)
        if first.startswith(b"\x89PNG\r\n\x1a\n") and first[12:16] == b"IHDR":
            return ".png"
        if first.startswith(b"\xff\xd8\xff"):
            file.seek(-2, os.SEEK_END)
            if file.read() == b"\xff\xd9":
                return ".jpg"
    raise ValueError("Generated cover must be a PNG or JPEG image.")


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
