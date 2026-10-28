import tempfile
from pathlib import Path

from flask import flash, request
from werkzeug.utils import secure_filename
from app.agents.finance_agent import ask_finance_agent
from app.services.pipeline import process_document
from app.services.document_reader import DocumentReadError
from app.services.extractor import ExtractionError
from app.services.pipeline import ProcessingError
from app.services.validator import validate_transaction
from flask import (
    Flask, jsonify, redirect, render_template, session,
    url_for,
)

from .config import Config
from .db import get_connection, init_db
from .routes.auth import auth_bp
from .services.transactions import (
    list_transactions,
    get_transaction,
    update_transaction,
    update_line_item,
    confirm_transaction,
    get_clarifications,
    answer_clarification,
)

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    if not app.config["SECRET_KEY"]:
        raise RuntimeError(
            "SECRET_KEY is not set. Copy .env.example to .env and set a value."
        )

    app.config["UPLOAD_FOLDER"].mkdir(parents=True, exist_ok=True)
    init_db(app.config["DATABASE_PATH"])
    app.register_blueprint(auth_bp)
    
    @app.route("/upload", methods=["GET", "POST"])
    def upload_receipt():
        if "user_id" not in session:
            return redirect(url_for("auth.login"))

        if request.method == "GET":
            return render_template("upload.html")

        uploaded = request.files.get("receipt")

        if not uploaded or not uploaded.filename:
            flash("Please choose a receipt file.", "error")
            return redirect(url_for("upload_receipt"))

        original_name = secure_filename(uploaded.filename)
        extension = Path(original_name).suffix.lower()
        allowed_extensions = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}

        if extension not in allowed_extensions:
            flash("Please upload a PDF, PNG, JPG, or WEBP file.", "error")
            return redirect(url_for("upload_receipt"))

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir) / f"receipt{extension}"
                uploaded.save(temp_path)

                conn = get_connection(app.config["DATABASE_PATH"])
                try:
                    result = process_document(
                        conn=conn,
                        user_id=session["user_id"],
                        file_path=temp_path,
                        original_filename=original_name,
                        upload_root=app.config["UPLOAD_FOLDER"],
                    )
                finally:
                    conn.close()

            if result["status"] == "duplicate_file":
                flash(
                    "This exact file was already uploaded. "
                    "No new transaction was created.",
                    "warning",
                )
                return redirect(url_for("transactions_page"))

            for warning in result.get("warnings", []):
                flash(warning, "warning")

            if result.get("needs_clarification"):
                flash(
                    "Receipt processed. Please review the uncertain fields "
                    "and possible duplicate warnings.",
                    "warning",
                )
            else:
                flash("Receipt processed successfully.", "success")

            return redirect(
                url_for("review_transaction", txn_id=result["transaction_id"])
            )

        except (DocumentReadError, ExtractionError, ProcessingError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("upload_receipt"))
        except Exception:
            app.logger.exception("Receipt upload failed")
            flash(
                "The receipt could not be processed due to an internal error.",
                "error",
            )
            return redirect(url_for("upload_receipt"))

    @app.route("/")
    def index():
        if "user_id" not in session:
            return redirect(url_for("auth.login"))

        conn = get_connection(app.config["DATABASE_PATH"])
        try:
            user_id = session["user_id"]
            transactions = list_transactions(conn, user_id, limit=10)

            summary = conn.execute(
                """
                SELECT
                    COUNT(*) AS transaction_count,
                    COALESCE(SUM(total_minor), 0) AS total_spending,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END)
                        AS pending_count
                FROM transactions
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()

            return render_template(
                "dashboard.html",
                email=session.get("user_email", "User"),
                transactions=transactions,
                transaction_count=summary["transaction_count"],
                total_spending=f"₹{summary['total_spending'] / 100:,.2f}",
                pending_count=summary["pending_count"] or 0,
            )
        finally:
            conn.close()

    @app.route("/transactions")
    def transactions_page():
        if "user_id" not in session:
            return redirect(url_for("auth.login"))

        conn = get_connection(app.config["DATABASE_PATH"])
        try:
            transactions = list_transactions(
                conn, session["user_id"], limit=100
            )
            return render_template(
                "transactions.html",
                transactions=transactions,
            )
        finally:
            conn.close()

    @app.route("/transactions/<int:txn_id>")
    def review_transaction(txn_id):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))

        conn = get_connection(app.config["DATABASE_PATH"])
        try:
            txn = get_transaction(conn, session["user_id"], txn_id)
            if txn is None:
                return "Transaction not found", 404

            
            clarifications = get_clarifications(
                conn, session["user_id"], txn_id
            )

            issues = validate_transaction(txn)

            resolved_fields = {
                item["field_name"]
                for item in clarifications
                if item["status"] in {"answered", "unknown"}
            }

            issues = [
                issue for issue in issues
                if issue["field"] not in resolved_fields
            ]

            print("REVIEW ISSUES:", issues)
            print("UNCERTAIN FIELDS:", txn.get("uncertain_fields"))
            return render_template(
                "review.html",
                txn=txn,
                issues=issues,
                clarifications=clarifications,
            )
        finally:
            conn.close()

    
    @app.route(
        "/clarifications/<int:clarification_id>/answer",
        methods=["POST"],
    )
    def answer_clarification_route(clarification_id):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))

        conn = get_connection(app.config["DATABASE_PATH"])
        try:
            txn_id = request.form.get("txn_id", type=int)
            answer = request.form.get("answer", "").strip()
            status = request.form.get("status", "answered")

            if not txn_id:
                flash("Invalid transaction.", "error")
                return redirect(url_for("transactions_page"))

            if status == "answered" and not answer:
                flash("Please enter an answer.", "error")
                return redirect(
                    url_for("review_transaction", txn_id=txn_id)
                )

            if status not in {"answered", "unknown"}:
                flash("Invalid clarification status.", "error")
                return redirect(
                    url_for("review_transaction", txn_id=txn_id)
                )

            clarification = conn.execute(
                """
                SELECT id FROM clarifications
                WHERE id = ? AND transaction_id = ? AND user_id = ?
                """,
                (clarification_id, txn_id, session["user_id"]),
            ).fetchone()

            if clarification is None:
                return "Clarification not found", 404

            saved = answer_clarification(
                conn,
                session["user_id"],
                clarification_id,
                answer if status == "answered" else None,
                status=status,
            )

            if not saved:
                return "Clarification not found", 404

            flash("Clarification updated successfully.", "success")
            return redirect(
                url_for("review_transaction", txn_id=txn_id)
            )
        finally:
            conn.close()

    
    @app.route("/transactions/<int:txn_id>/edit", methods=["POST"])
    def edit_transaction(txn_id):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))

        conn = get_connection(app.config["DATABASE_PATH"])
        try:
            changes = {
                "merchant": request.form.get("merchant", "").strip(),
                "txn_date": request.form.get("txn_date", "").strip(),
                "currency": request.form.get("currency", "INR").strip(),
                "payment_method": request.form.get("payment_method", "").strip() or None,
            }

            total = request.form.get("total", "").strip()
            if not total:
                flash("Total amount is required.", "error")
                return redirect(url_for("review_transaction", txn_id=txn_id))

            try:
                amount = float(total)
                if amount < 0:
                    raise ValueError
                changes["total_minor"] = round(amount * 100)
            except ValueError:
                flash("Enter a valid non-negative total amount.", "error")
                return redirect(url_for("review_transaction", txn_id=txn_id))

            updated = update_transaction(
                conn, session["user_id"], txn_id, changes
            )

            if updated is None:
                return "Transaction not found", 404

            flash("Changes saved successfully.", "success")
            return redirect(url_for("review_transaction", txn_id=txn_id))
        except Exception:
            app.logger.exception("Transaction edit failed")
            flash("Could not save changes. Please check the entered values.", "error")
            return redirect(url_for("review_transaction", txn_id=txn_id))
        finally:
            conn.close()

    
    @app.route("/transactions/<int:txn_id>/confirm", methods=["POST"])
    def confirm_transaction_route(txn_id):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))

        conn = get_connection(app.config["DATABASE_PATH"])
        try:
            try:
                confirmed = confirm_transaction(
                    conn, session["user_id"], txn_id
                )
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(
                    url_for("review_transaction", txn_id=txn_id)
                )

            if not confirmed:
                return "Transaction not found", 404

            flash("Transaction confirmed successfully.", "success")
            return redirect(url_for("review_transaction", txn_id=txn_id))
        finally:
            conn.close()


    @app.route(
        "/transactions/<int:txn_id>/items/<int:item_id>/edit",
        methods=["POST"],
    )
    def edit_line_item_route(txn_id, item_id):
        if "user_id" not in session:
            return redirect(url_for("auth.login"))

        conn = get_connection(app.config["DATABASE_PATH"])
        try:
            # Confirm the item belongs to this user's transaction.
            txn = get_transaction(conn, session["user_id"], txn_id)
            if txn is None:
                return "Transaction not found", 404

            item = next(
                (i for i in txn["items"] if i["id"] == item_id),
                None,
            )
            if item is None:
                return "Line item not found", 404

            name = request.form.get("name", "").strip()
            quantity_text = request.form.get("quantity", "").strip()
            unit_price_text = request.form.get("unit_price", "").strip()
            item_total_text = request.form.get("item_total", "").strip()

            if not name:
                flash("Item name is required.", "error")
                return redirect(url_for("review_transaction", txn_id=txn_id))

            try:
                quantity = float(quantity_text)
                unit_price = float(unit_price_text)
                item_total = float(item_total_text)

                if quantity <= 0 or unit_price < 0 or item_total < 0:
                    raise ValueError
            except ValueError:
                flash(
                    "Enter a valid quantity and non-negative prices.",
                    "error",
                )
                return redirect(url_for("review_transaction", txn_id=txn_id))

            changes = {
                "name": name,
                "quantity": quantity,
                "unit_price_minor": round(unit_price * 100),
                "total_minor": round(item_total * 100),
            }

            changed = update_line_item(
                conn, session["user_id"], item_id, changes
            )

            if changed is None:
                return "Line item not found", 404

            flash("Line item updated successfully.", "success")
            return redirect(url_for("review_transaction", txn_id=txn_id))

        except Exception:
            app.logger.exception("Line item update failed")
            flash("Could not update this item. Check the entered values.", "error")
            return redirect(url_for("review_transaction", txn_id=txn_id))
        finally:
            conn.close()


    
    @app.route("/ask-finance", methods=["GET", "POST"])
    def ask_finance():
        if "user_id" not in session:
            return redirect(url_for("auth.login"))

        answer = None
        question = ""

        if request.method == "POST":
            question = request.form.get("question", "").strip()

            if not question:
                flash("Please enter a finance question.", "error")
            else:
                try:
                    answer = ask_finance_agent(
                        question=question,
                        user_id=session["user_id"],
                        db_path=app.config["DATABASE_PATH"],
                        api_key=app.config["GEMINI_API_KEY"],
                        model=app.config["GEMINI_MODEL"],
                    )
                except Exception:
                    app.logger.exception("Finance agent request failed")
                    flash(
                        "The finance assistant could not answer. "
                        "Check the server terminal for details.",
                        "error",
                    )

        return render_template(
            "finance_chat.html",
            question=question,
            answer=answer,
        )

    @app.route("/health")
    def health():
        return jsonify(
            status="ok",
            app="Finance Manager AI Agent",
            llm_provider=app.config["LLM_PROVIDER"],
            llm_key_configured=bool(app.config["GEMINI_API_KEY"]),
        )

    return app
