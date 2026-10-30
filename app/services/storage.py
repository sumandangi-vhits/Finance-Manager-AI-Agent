import hashlib
import mimetypes
import shutil
from pathlib import Path
from app.config import Config

def hash_file(path):
    """SHA-256 fingerprint of a file's contents."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def guess_mime_type(filename):
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


def save_original(user_id, src_path, file_hash, upload_root=None):
    """Copy the uploaded file into the user's private folder.
    The stored name comes from the hash, never from the user's filename.
    Returns (relative_path, newly_created)."""
    root = Path(upload_root or Config.UPLOAD_FOLDER)
    suffix = Path(src_path).suffix.lower()
    relative = Path(str(user_id)) / f"{file_hash[:32]}{suffix}"
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)

    created = not destination.exists()
    if created:
        shutil.copyfile(src_path, destination)
    return relative.as_posix(), created