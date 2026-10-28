from pathlib import Path

from pydantic import ValidationError

from app.config import Config
from app.models.schemas import TransactionIn
from app.services import documents as documents_service
from app.services import transactions as transactions_service
from app.services.document_reader import read_document
from app.services.duplicates import find_same_file, find_similar_transactions
from app.services.extractor import extract_receipt, to_transaction_dict
from app.services.storage import guess_mime_type, hash_file, save_original
from app.services.validator import validate_transaction


class ProcessingError(Exception):
    """Raised with a user-friendly message when extracted data cannot be used."""


def process_document(conn, user_id, file_path, original_filename,
                     allow_duplicate=False, client=None, upload_root=None):
    """Upload workflow: read -> extract -> validate -> check duplicates -> store -> save.

    Returns a dict. status is "duplicate_file" (nothing saved) or "saved".
    Raises DocumentReadError, ExtractionError or ProcessingError with a friendly
    message; nothing is saved in those cases."""
    file_path = Path(file_path)
    root = Path(upload_root or Config.UPLOAD_FOLDER)
    file_hash = hash_file(file_path)

    # 1. Exact same file already uploaded? Stop and let the user decide.
    same = find_same_file(conn, user_id, file_hash)
    if same and not allow_duplicate:
        return {"status": "duplicate_file", "existing": same}

    # 2. Read and extract. Nothing is written to disk or database yet.
    doc = read_document(file_path)
    receipt = extract_receipt(doc["text"], client=client)
    data = to_transaction_dict(receipt)

    # 3. The values must be usable (no negative amounts, zero quantities, ...)
    try:
        TransactionIn(**data)
    except ValidationError as exc:
        raise ProcessingError(
            "The extracted values could not be used (for example a negative amount "
            "or a zero quantity). Please check the document."
        ) from exc

    # 4. Validate and look for look-alike transactions
    issues = validate_transaction(data)
    similar = find_similar_transactions(conn, user_id, data)
    if similar:
        ids = ", ".join(f"#{t['id']}" for t in similar)
        issues.append({
            "field": "duplicate",
            "code": "possible_duplicate",
            "message": (f"This looks similar to transaction {ids} that you already "
                        "saved. Is it a duplicate or a separate purchase?"),
        })

    data["uncertain_fields"] = sorted(
        set(data["uncertain_fields"])
        | {i["field"] for i in issues if i["field"] != "duplicate"}
    )

    # 5. Store the original file, then save the records
    stored_path, created = save_original(user_id, file_path, file_hash, root)
    document_id = None
    try:
        document_id = documents_service.create_document(
            conn, user_id, Path(original_filename).name, stored_path,
            file_hash, guess_mime_type(original_filename), doc["page_count"],
        )
        transaction_id = transactions_service.create_transaction(
            conn, user_id, data, document_id=document_id
        )
        
        for issue in issues:
            if issue["field"] == "duplicate":
                continue

            transactions_service.create_clarification(
                conn,
                user_id,
                transaction_id,
                issue["field"],
                issue["message"],
            )

    except Exception:
        if document_id is not None:
            with conn:
                conn.execute(
                    "DELETE FROM documents WHERE id = ? AND user_id = ?",
                    (document_id, user_id),
                )
        if created:
            (root / stored_path).unlink(missing_ok=True)
        raise

    return {
        "status": "saved",
        "transaction_id": transaction_id,
        "document_id": document_id,
        "issues": issues,
        "needs_clarification": bool(issues),
        "warnings": doc["warnings"],
        "read_method": doc["method"],
    }