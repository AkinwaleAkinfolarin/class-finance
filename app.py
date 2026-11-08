from flask import (
    Flask,
    render_template,
    request,
    redirect,
    session,
    flash,
    url_for,
    send_from_directory
)

from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from database import get_connection, initialize_database
import ai_reports
from PIL import Image
import pytesseract
import os
import re
import math
import uuid
import sqlite3
import cloudinary
import cloudinary.uploader
from groq import Groq
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment
from psycopg2 import IntegrityError as PostgreSQLIntegrityError
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

# =========================================================
# APP CONFIGURATION
# =========================================================
import time
from flask import Flask, request, jsonify
from groq import Groq, RateLimitError

app = Flask(__name__)

# We name this client 'groq_client' to match your setup
groq_client = Groq()

def get_finance_summary():
    connection = get_connection()

    total_students = connection.execute(
        "SELECT COUNT(*) AS count FROM students"
    ).fetchone()["count"]

    total_payments = connection.execute(
        "SELECT COUNT(*) AS count FROM payments"
    ).fetchone()["count"]

    verified_amount = connection.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM payments
        WHERE status = 'Verified'
        """
    ).fetchone()["total"]

    pending_amount = connection.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM payments
        WHERE status IN ('Pending', 'Review')
        """
    ).fetchone()["total"]

    flagged_amount = connection.execute(
        """
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM payments
        WHERE status = 'Flagged'
        """
    ).fetchone()["total"]

    connection.close()

    return {
        "total_students": total_students,
        "total_payments": total_payments,
        "verified_amount": verified_amount,
        "pending_amount": pending_amount,
        "flagged_amount": flagged_amount
    }


def generate_manual_purchase_list(purpose):
    """Return all Active students with a Verified payment for a purpose."""

    purpose_result = ai_reports.resolve_payment_purpose(purpose)

    if purpose_result["status"] != "matched":
        return []

    report = ai_reports.get_purpose_payment_report(
        purpose_result["purpose"]["id"]
    )

    if not report:
        return []

    return [
        {
            "matric_number": student["matric_number"],
            "full_name": student["full_name"],
            "verified_paid": float(student["verified_paid"] or 0)
        }
        for student in report["verified_payers"]
    ]


def ask_finance_ai(question):
    """Build accurate, read-only finance reports from database records."""

    question_lower = question.casefold()

    def money(value):
        return f"₦{float(value or 0):,.2f}"

    def make_table(headers, rows):
        output = ["[TABLE]", " | ".join(headers)]

        for row in rows:
            output.append(" | ".join(str(value) for value in row))

        output.append("[/TABLE]")
        return "\n".join(output)

    # -----------------------------------------------------
    # INDIVIDUAL STUDENT PAYMENT HISTORY
    # -----------------------------------------------------
    history_words = (
        "payment history",
        "payment record",
        "payment records",
        "history of",
        "what has",
        "what did",
        "payments made by",
    )

    if any(word in question_lower for word in history_words):
        match = ai_reports.find_student_in_question(question)

        if match["status"] == "ambiguous":
            return (
                "I found multiple matching students. Please use the "
                "student's exact matric number to identify the correct record.\n\n"
                + make_table(
                    ["Matric Number", "Student Name"],
                    [
                        [s["matric_number"], s["full_name"]]
                        for s in match["students"]
                    ]
                )
            )

        if match["status"] == "not_found":
            return (
                "I couldn't identify an Active student from that question. "
                "Please include the student's exact matric number."
            )

        history = ai_reports.get_student_payment_history(
            match["student"]["id"]
        )

        if not history:
            return "The student's payment history could not be retrieved."

        student = history["student"]
        records = history["payments"]

        answer = (
            f"## Payment History\n\n"
            f"**Student:** {student['full_name']}\n\n"
            f"**Matric Number:** {student['matric_number']}\n\n"
            f"**Payment records:** {history['payment_count']}\n\n"
            f"**Verified records:** {history['verified_count']}\n\n"
        )

        if records:
            answer += make_table(
                [
                    "S/N",
                    "Payment Purpose",
                    "Amount",
                    "Date",
                    "Status",
                    "Receipt Reference",
                ],
                [
                    [
                        index,
                        item["recognised_purpose"] or item["recorded_purpose"] or "Not specified",
                        money(item["amount"]),
                        item["payment_date"],
                        item["status"],
                        item["receipt_reference"] or "—",
                    ]
                    for index, item in enumerate(records, start=1)
                ]
            )
        else:
            answer += "No payment records were found for this student."

        return answer

    # -----------------------------------------------------
    # PAYMENT STATUS SUMMARY
    # -----------------------------------------------------
    status_words = (
        "payment status",
        "status summary",
        "pending payments",
        "review payments",
        "flagged payments",
        "voided payments",
        "verified payments",
        "all payment statuses",
    )

    if any(word in question_lower for word in status_words):
        statuses = ai_reports.get_payment_status_summary()

        rows = [
            [
                status,
                statuses[status]["payment_count"],
                money(statuses[status]["total_amount"]),
            ]
            for status in ai_reports.PAYMENT_STATUSES
        ]

        total_count = sum(
            item["payment_count"] for item in statuses.values()
        )
        total_amount = sum(
            item["total_amount"] for item in statuses.values()
        )

        return (
            "## Payment Status Summary\n\n"
            + make_table(
                ["Status", "Number of Records", "Total Amount"],
                rows
            )
            + f"\n\n**Total records:** {total_count}"
            + f"\n\n**Total submitted amount:** {money(total_amount)}"
            + "\n\nOnly Verified payments count as collected funds."
        )

    # -----------------------------------------------------
    # PAYMENT PURPOSE REPORT
    # -----------------------------------------------------
    purpose_result = ai_reports.resolve_payment_purpose(question)

    if purpose_result["status"] == "ambiguous":
        return (
            "Your question matches more than one payment purpose. "
            "Please specify which one you mean.\n\n"
            + make_table(
                ["Available Payment Purpose"],
                [[item["name"]] for item in purpose_result["purposes"]]
            )
        )

    if purpose_result["status"] == "matched":
        purpose = purpose_result["purpose"]
        report = ai_reports.get_purpose_payment_report(purpose["id"])

        if not report:
            return "I couldn't retrieve the report for that payment purpose."

        answer = (
            f"## {report['purpose']} — Payment Report\n\n"
            f"**Expected fee per student:** {money(report['expected_amount_per_student'])}\n\n"
            f"**Active students:** {report['active_students']}\n\n"
            f"**Expected total:** {money(report['expected_total'])}\n\n"
            f"**Verified collected:** {money(report['verified_collected'])}\n\n"
            f"**Outstanding balance:** {money(report['outstanding_total'])}\n\n"
            f"**Fully paid:** {report['paid_count']}\n\n"
            f"**Outstanding:** {report['outstanding_count']}\n\n"
            f"**Partially paid:** {report['partial_count']}\n\n"
        )

        if any(
            word in question_lower
            for word in ("outstanding", "owe", "owing", "unpaid", "not paid")
        ):
            rows = [
                [
                    index,
                    item["matric_number"],
                    item["full_name"],
                    money(item["verified_paid"]),
                    money(item["balance"]),
                ]
                for index, item in enumerate(
                    report["outstanding_students"], start=1
                )
            ]

            answer += "**Students with outstanding balances**\n\n"

            answer += make_table(
                [
                    "S/N",
                    "Matric Number",
                    "Student Name",
                    "Verified Paid",
                    "Balance",
                ],
                rows
            )

        elif any(
            word in question_lower
            for word in ("who paid", "who has paid", "has paid",
                         "list paid", "paid students",
                         "verified payers", "purchase list")
        ):
            rows = [
                [
                    index,
                    item["matric_number"],
                    item["full_name"],
                    money(item["verified_paid"]),
                ]
                for index, item in enumerate(
                    report["verified_payers"], start=1
                )
            ]

            answer += "**Verified payers**\n\n"

            answer += make_table(
                ["S/N", "Matric Number", "Student Name", "Verified Paid"],
                rows
            )

        return answer

    # -----------------------------------------------------
    # GENERAL FINANCIAL SUMMARY
    # -----------------------------------------------------
    summary = get_finance_summary()
    statuses = ai_reports.get_payment_status_summary()

    active_connection = get_connection()

    try:
        active_students = active_connection.execute("""
            SELECT COUNT(*) AS count
            FROM students
            WHERE status = 'Active'
        """).fetchone()["count"]
    finally:
        active_connection.close()

    return (
        "## Class Finance Summary\n\n"
        f"**Active students:** {active_students}\n\n"
        f"**Total payment records:** {summary['total_payments']}\n\n"
        f"**Verified collected:** {money(statuses['Verified']['total_amount'])}\n\n"
        f"**Pending:** {money(statuses['Pending']['total_amount'])}\n\n"
        f"**Under review:** {money(statuses['Review']['total_amount'])}\n\n"
        f"**Flagged:** {money(statuses['Flagged']['total_amount'])}\n\n"
        f"**Voided:** {money(statuses['Voided']['total_amount'])}\n\n"
        "Only Verified payments count as collected funds."
    )


def ask_groq(question):
    response = groq_client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {
                "role": "system",
                "content": "You are the Class Finance AI Assistant. Give clear, concise answers."
            },
            {
                "role": "user",
                "content": question
            }
        ]
    )

    return response.choices[0].message.content
initialize_database()

from import_students import import_students
import_students()
# =========================================================
# PHASE 4 — OCR RECEIPT READER
# =========================================================

def extract_receipt_ocr(filepath):

    try:
        image = Image.open(filepath)

        # Convert image to RGB first
        if image.mode != "RGB":
            image = image.convert("RGB")

        # Convert to grayscale
        image = image.convert("L")

        # Resize for better OCR performance
        image.thumbnail((1200, 1200))

        # Increase contrast
        from PIL import ImageEnhance

        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(2.0)

        # OCR
        os.environ["OMP_THREAD_LIMIT"] = "1"

        text = pytesseract.image_to_string(
            image,
            config="--psm 6",
            timeout=10
        )

        return text.strip()

    except RuntimeError:
        print("OCR TIMEOUT — continuing without OCR")
        return ""

    except Exception as error:
        print("OCR ERROR:", error)
        return ""
# =========================================================
# PHASE 4.2 — OCR FIELD EXTRACTION
# =========================================================

def extract_receipt_fields(ocr_text):

    fields = {
        "ocr_amount": None,
        "ocr_date": None,
        "ocr_reference": None,
        "ocr_sender": None,
        "ocr_recipient": None,
        "ocr_narration": None
    }

    if not ocr_text:
        return fields

    # Clean OCR text
    text = ocr_text.strip()

    # =====================================================
    # AMOUNT
    # =====================================================

    amount_match = re.search(
        r"(?:₦|NGN|\$)\s*([\d,]+(?:\.\d{2})?)",
        text,
        re.IGNORECASE
    )

    if not amount_match:
        amount_match = re.search(
            r"\b([\d,]+\.\d{2})\b",
            text
        )

    if amount_match:

        try:
            fields["ocr_amount"] = float(
                amount_match.group(1).replace(",", "")
            )
        except ValueError:
            pass

    # =====================================================
    # DATE
    # =====================================================

    date_match = re.search(
        r"([A-Z][a-z]+\s+[0-9S]\w*"
        r",?\s+\d{4}"
        r"(?:\s+\d{1,2}:\d{2}:\d{2})?)",
        text
    )

    if date_match:
        fields["ocr_date"] = date_match.group(1).strip()

   # =====================================================
   # TRANSACTION REFERENCE
   # =====================================================

    reference_match = re.search(
        r"Transaction\s*(?:No\.?|ID|Reference)"
        r"\s*[:#]?\s*([A-Za-z0-9\-]+)",
        text,
        re.IGNORECASE
    )

    if reference_match:
        fields["ocr_reference"] = (
            reference_match.group(1).strip()
        )
    # =====================================================
    # SENDER
    # =====================================================

    sender_match = re.search(
        r"Sender\s+Details\s+([^\n]+)",
        text,
        re.IGNORECASE
    )

    if sender_match:
        fields["ocr_sender"] = (
            sender_match.group(1).strip()
        )

    # =====================================================
    # RECIPIENT
    # =====================================================

    recipient_match = re.search(
        r"Recipient\s+Details\s+([^\n]+)",
        text,
        re.IGNORECASE
    )

    if recipient_match:
        fields["ocr_recipient"] = (
            recipient_match.group(1).strip()
        )

    # =====================================================
    # NARRATION / REMARK
    # =====================================================

    narration_match = re.search(
        r"(?:Remark|Narration|Description)"
        r"\s+([^\n]+)",
        text,
        re.IGNORECASE
    )

    if narration_match:
        fields["ocr_narration"] = (
            narration_match.group(1).strip()
        )

    # =====================================================
    # RETURN STRUCTURED OCR DATA
    # =====================================================

    return fields
# =========================================================
# PHASE 4.3 — AI/OCR PAYMENT VERIFICATION
# =========================================================

def verify_payment_with_ocr(
    submitted_amount,
    student_name,
    purpose,
    ocr_fields
):

    checks = []
    score = 0
    total_checks = 0

    # =====================================================
    # AMOUNT CHECK
    # =====================================================

    ocr_amount = ocr_fields.get("ocr_amount")

    if ocr_amount is not None:

        total_checks += 1

        try:
            if abs(
                float(submitted_amount) -
                float(ocr_amount)
            ) < 0.01:

                score += 1
                checks.append(
                    "Amount matches receipt."
                )

            else:

                checks.append(
                    f"Amount mismatch: "
                    f"submitted ₦{float(submitted_amount):,.2f}, "
                    f"receipt shows ₦{float(ocr_amount):,.2f}."
                )

        except (ValueError, TypeError):

            checks.append(
                "Could not compare payment amount."
            )

    else:

        checks.append("Receipt amount could not be detected by OCR."
        )
    # =====================================================
    # STUDENT / SENDER CHECK
    # =====================================================

    ocr_sender = ocr_fields.get(
        "ocr_sender"
    )

    if ocr_sender:

        total_checks += 1

        student_clean = re.sub(
            r"[^a-z0-9 ]",
            "",
            student_name.lower()
        )

        sender_clean = re.sub(
            r"[^a-z0-9 ]",
            "",
            ocr_sender.lower()
        )

        student_words = set(
            student_clean.split()
        )

        sender_words = set(
            sender_clean.split()
        )

        common_words = (
            student_words & sender_words
        )

        if len(common_words) >= 2:

            score += 1
            checks.append(
                "Receipt sender matches the student name."
            )

        else:

            checks.append(
                "Receipt sender does not clearly match the student name."
            )

    else:

        checks.append(
            "Receipt sender could not be detected by OCR."
        )
    # =====================================================
    # NARRATION / PURPOSE CHECK
    # =====================================================

    ocr_narration = ocr_fields.get(
        "ocr_narration"
    )

    if ocr_narration and purpose:

        total_checks += 1

        purpose_words = set(
            re.sub(
                r"[^a-z0-9 ]",
                "",
                purpose.lower()
            ).split()
        )

        narration_words = set(
            re.sub(
                r"[^a-z0-9 ]",
                "",
                ocr_narration.lower()
            ).split()
        )

        common_purpose_words = (
            purpose_words & narration_words
        )

        if common_purpose_words:

            score += 1
            checks.append(
                "Receipt narration is related to the payment purpose."
            )

        else:

            checks.append(
                "Receipt narration does not clearly match the payment purpose."
            )

    else:

        checks.append(
            "Receipt narration could not be verified."
        )

    # =====================================================
    # DETERMINE VERIFICATION RESULT
    # =====================================================

    if total_checks == 0:

        verification = "Review"

        note = (
            "OCR could not verify any payment details. "
            "Manual review is required."
        )

    else:

        percentage = (
            score / total_checks
        ) * 100

        if percentage == 100:

            verification = "Verified"

            note = (
                "OCR verification passed. "
                "All detected payment details match."
            )

        elif percentage >= 50:

            verification = "Review"

            note = (
                "Some payment details matched, "
                "but manual review is recommended."
            )

        else:

            verification = "Flagged"

            note = (
                "OCR verification found significant "
                "mismatches in the receipt details."
            )

    # =====================================================
    # RETURN VERIFICATION RESULT
    # =====================================================

    return {
        "verification": verification,
        "score": score,
        "total_checks": total_checks,
        "checks": checks,
        "note": note
    }
app.secret_key = os.environ.get("SECRET_KEY")

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True
)
# Maximum uploaded receipt size: 5 MB
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024

# Receipt storage directory
UPLOAD_FOLDER = "uploads/receipts"

# Allowed receipt file types
ALLOWED_EXTENSIONS = {
    "jpg",
    "jpeg",
    "png",
    "pdf"
}


# =========================================================
# HELPER FUNCTIONS
# =========================================================

def allowed_file(filename):

    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in ALLOWED_EXTENSIONS
    )
# =========================================================
# ADMIN LOGIN
# =========================================================

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():

    if request.method == "POST":

        username = request.form.get(
            "username",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        connection = get_connection()

        admin = connection.execute(
            """
            SELECT id, username, password_hash, full_name
            FROM admins
            WHERE username = ?
              AND status = 'Active'
            """,
            (username,)
        ).fetchone()

        connection.close()

        if admin and check_password_hash(
            admin["password_hash"],
            password
        ):

            session["admin_logged_in"] = True
            session["admin_id"] = admin["id"]
            session["admin_username"] = admin["username"]
            session["admin_full_name"] = admin["full_name"]

            return redirect(
                url_for("home")
            )

        return render_template(
            "admin_login.html",
            error="Invalid username or password."
        )

    return render_template(
        "admin_login.html"
    )


@app.route("/admin/communications")
def admin_communications():

    access = admin_required()

    if access:
        return access

    connection = get_connection()

    announcements = connection.execute(
        """
        SELECT
            id,
            title,
            message,
            payment_purpose,
            start_date,
            deadline_date,
            status,
            priority,
            created_at
        FROM announcements
        ORDER BY priority DESC, created_at DESC
        """
    ).fetchall()

    advertisements = connection.execute(
        """
        SELECT
            id,
            title,
            description,
            image_url,
            target_url,
            start_date,
            end_date,
            status,
            priority,
            created_at
        FROM advertisements
        ORDER BY priority DESC, created_at DESC
        """
    ).fetchall()

    connection.close()

    return render_template(
        "admin_communications.html",
        announcements=announcements,
        advertisements=advertisements
    )


@app.route("/admin/communications/announcement/add", methods=["POST"])
def add_announcement():

    access = admin_required()

    if access:
        return access

    title = request.form.get("title", "").strip()
    message = request.form.get("message", "").strip()
    payment_purpose = request.form.get("payment_purpose", "").strip() or None
    start_date = request.form.get("start_date", "").strip() or None
    deadline_date = request.form.get("deadline_date", "").strip() or None
    status = request.form.get("status", "Active").strip()
    priority_raw = request.form.get("priority", "0").strip()

    try:
        priority = int(priority_raw)
    except ValueError:
        priority = 0

    if not title or not message:
        return redirect(url_for("admin_communications"))

    connection = get_connection()

    connection.execute(
        """
        INSERT INTO announcements (
            title,
            message,
            payment_purpose,
            start_date,
            deadline_date,
            status,
            priority
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            message,
            payment_purpose,
            start_date,
            deadline_date,
            status,
            priority
        )
    )

    connection.commit()
    connection.close()

    return redirect(url_for("admin_communications"))
@app.route("/admin/communications/announcement/edit/<int:announcement_id>", methods=["GET", "POST"])
def edit_announcement(announcement_id):

    access = admin_required()

    if access:
        return access

    connection = get_connection()

    if request.method == "POST":

        title = request.form.get("title", "").strip()
        message = request.form.get("message", "").strip()
        payment_purpose = request.form.get("payment_purpose", "").strip() or None
        start_date = request.form.get("start_date", "").strip() or None
        deadline_date = request.form.get("deadline_date", "").strip() or None
        status = request.form.get("status", "Active").strip()
        priority_raw = request.form.get("priority", "0").strip()

        try:
            priority = int(priority_raw)
        except ValueError:
            priority = 0

        connection.execute(
            """
            UPDATE announcements
            SET
                title = ?,
                message = ?,
                payment_purpose = ?,
                start_date = ?,
                deadline_date = ?,
                status = ?,
                priority = ?
            WHERE id = ?
            """,
            (
                title,
                message,
                payment_purpose,
                start_date,
                deadline_date,
                status,
                priority,
                announcement_id
            )
        )

        connection.commit()
        connection.close()

        return redirect(url_for("admin_communications"))

    announcement = connection.execute(
        """
        SELECT
            id,
            title,
            message,
            payment_purpose,
            start_date,
            deadline_date,
            status,
            priority
        FROM announcements
        WHERE id = ?
        """,
        (announcement_id,)
    ).fetchone()

    connection.close()

    if not announcement:
        return redirect(url_for("admin_communications"))

    return render_template(
        "edit_announcement.html",
        announcement=announcement
    )


@app.route("/admin/communications/announcement/toggle/<int:announcement_id>", methods=["POST"])
def toggle_announcement(announcement_id):

    access = admin_required()

    if access:
        return access

    connection = get_connection()

    announcement = connection.execute(
        """
        SELECT status
        FROM announcements
        WHERE id = ?
        """,
        (announcement_id,)
    ).fetchone()

    if announcement:

        new_status = (
            "Inactive"
            if announcement["status"] == "Active"
            else "Active"
        )

        connection.execute(
            """
            UPDATE announcements
            SET status = ?
            WHERE id = ?
            """,
            (new_status, announcement_id)
        )

        connection.commit()

    connection.close()

    return redirect(url_for("admin_communications"))


@app.route("/admin/communications/announcement/delete/<int:announcement_id>", methods=["POST"])
def delete_announcement(announcement_id):

    access = admin_required()

    if access:
        return access

    connection = get_connection()

    connection.execute(
        """
        DELETE FROM announcements
        WHERE id = ?
        """,
        (announcement_id,)
    )

    connection.commit()
    connection.close()

    return redirect(url_for("admin_communications"))

@app.route("/admin/communications/advertisement/add", methods=["POST"])
def add_advertisement():

    access = admin_required()

    if access:
        return access

    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    advertisement_image = request.files.get("advertisement_image")
    target_url = request.form.get("target_url", "").strip() or None
    start_date = request.form.get("start_date", "").strip() or None
    end_date = request.form.get("end_date", "").strip() or None
    status = request.form.get("status", "Active").strip()
    priority_raw = request.form.get("priority", "0").strip()

    try:
        priority = int(priority_raw)
    except ValueError:
        priority = 0

    if not title:
        return redirect(url_for("admin_communications"))

    image_url = None

    if advertisement_image and advertisement_image.filename:
        clean_filename = secure_filename(
            advertisement_image.filename
        )

        if not allowed_file(clean_filename):
            return (
                "Invalid advertisement image type. "
                "Only JPG, JPEG, PNG and WEBP are allowed.",
                400
            )

        advertisement_path = os.path.join(
            UPLOAD_FOLDER,
            f"{uuid.uuid4().hex}_{clean_filename}"
        )

        os.makedirs(
            UPLOAD_FOLDER,
            exist_ok=True
        )

        advertisement_image.save(advertisement_path)

        cloudinary_result = cloudinary.uploader.upload(
            advertisement_path,
            folder="class_finance/advertisements",
            resource_type="image"
        )

        image_url = cloudinary_result["secure_url"]

        os.remove(advertisement_path)

    connection = get_connection()

    connection.execute(
        """
        INSERT INTO advertisements (
            title,
            description,
            image_url,
            target_url,
            start_date,
            end_date,
            status,
            priority
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            description,
            image_url,
            target_url,
            start_date,
            end_date,
            status,
            priority
        )
    )

    connection.commit()
    connection.close()

    return redirect(url_for("admin_communications"))

@app.route("/admin/communications/advertisement/edit/<int:advertisement_id>", methods=["GET", "POST"])
def edit_advertisement(advertisement_id):

    access = admin_required()

    if access:
        return access

    connection = get_connection()

    if request.method == "POST":

        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        target_url = request.form.get("target_url", "").strip() or None
        start_date = request.form.get("start_date", "").strip() or None
        end_date = request.form.get("end_date", "").strip() or None
        status = request.form.get("status", "Active").strip()
        priority_raw = request.form.get("priority", "0").strip()

        try:
            priority = int(priority_raw)
        except ValueError:
            priority = 0

        advertisement_image = request.files.get("advertisement_image")

        image_url = None

        if advertisement_image and advertisement_image.filename:

            clean_filename = secure_filename(
                advertisement_image.filename
            )

            if not allowed_file(clean_filename):
                connection.close()

                return (
                    "Invalid advertisement image type. "
                    "Only JPG, JPEG, PNG and WEBP are allowed.",
                    400
                )

            advertisement_path = os.path.join(
                UPLOAD_FOLDER,
                f"{uuid.uuid4().hex}_{clean_filename}"
            )

            os.makedirs(
                UPLOAD_FOLDER,
                exist_ok=True
            )

            advertisement_image.save(advertisement_path)

            cloudinary_result = cloudinary.uploader.upload(
                advertisement_path,
                folder="class_finance/advertisements",
                resource_type="image"
            )

            image_url = cloudinary_result["secure_url"]

            os.remove(advertisement_path)

        if image_url:

            connection.execute(
                """
                UPDATE advertisements
                SET
                    title = ?,
                    description = ?,
                    image_url = ?,
                    target_url = ?,
                    start_date = ?,
                    end_date = ?,
                    status = ?,
                    priority = ?
                WHERE id = ?
                """,
                (
                    title,
                    description,
                    image_url,
                    target_url,
                    start_date,
                    end_date,
                    status,
                    priority,
                    advertisement_id
                )
            )

        else:

            connection.execute(
                """
                UPDATE advertisements
                SET
                    title = ?,
                    description = ?,
                    target_url = ?,
                    start_date = ?,
                    end_date = ?,
                    status = ?,
                    priority = ?
                WHERE id = ?
                """,
                (
                    title,
                    description,
                    target_url,
                    start_date,
                    end_date,
                    status,
                    priority,
                    advertisement_id
                )
            )

        connection.commit()
        connection.close()

        return redirect(url_for("admin_communications"))

    advertisement = connection.execute(
        """
        SELECT
            id,
            title,
            description,
            image_url,
            target_url,
            start_date,
            end_date,
            status,
            priority
        FROM advertisements
        WHERE id = ?
        """,
        (advertisement_id,)
    ).fetchone()

    connection.close()

    if not advertisement:
        return redirect(url_for("admin_communications"))

    return render_template(
        "edit_advertisement.html",
        advertisement=advertisement
    )


@app.route("/admin/communications/advertisement/toggle/<int:advertisement_id>", methods=["POST"])
def toggle_advertisement(advertisement_id):

    access = admin_required()

    if access:
        return access

    connection = get_connection()

    advertisement = connection.execute(
        """
        SELECT status
        FROM advertisements
        WHERE id = ?
        """,
        (advertisement_id,)
    ).fetchone()

    if advertisement:

        new_status = (
            "Inactive"
            if advertisement["status"] == "Active"
            else "Active"
        )

        connection.execute(
            """
            UPDATE advertisements
            SET status = ?
            WHERE id = ?
            """,
            (new_status, advertisement_id)
        )

        connection.commit()

    connection.close()

    return redirect(url_for("admin_communications"))


@app.route("/admin/communications/advertisement/delete/<int:advertisement_id>", methods=["POST"])
def delete_advertisement(advertisement_id):

    access = admin_required()

    if access:
        return access

    connection = get_connection()

    connection.execute(
        """
        DELETE FROM advertisements
        WHERE id = ?
        """,
        (advertisement_id,)
    )

    connection.commit()
    connection.close()

    return redirect(url_for("admin_communications"))
@app.route("/admin/logout")
def admin_logout():

    session.pop(
        "admin_logged_in",
        None
    )

    return redirect(
        url_for("admin_login")
    )


# =========================================================
# ADMIN ACCESS CHECK
# =========================================================

def admin_required():

    if not session.get(
        "admin_logged_in"
    ):

        return redirect(
            url_for("admin_login")
        )

    return None
# =========================================================
# ADMIN LOGIN
# =========================================================

# MAIN DASHBOARD
# =========================================================
@app.route("/export/manual/<path:purpose>")
def export_manual_excel(purpose):
    if "admin_id" not in session:
        return {"error": "Unauthorized"}, 401

    payment_list = generate_manual_purchase_list(purpose)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Manual Purchase List"

    sheet.append([
        "S/N",
        "Matric Number",
        "Student Name",
        "Verified Paid (₦)"
    ])

    for index, student in enumerate(payment_list, start=1):
        sheet.append([
            index,
            student["matric_number"],
            student["full_name"],
            student["verified_paid"]
        ])

    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")

    sheet.column_dimensions["A"].width = 8
    sheet.column_dimensions["B"].width = 20
    sheet.column_dimensions["C"].width = 32
    sheet.column_dimensions["D"].width = 20

    filename = "manual_purchase_list.xlsx"

    output_path = os.path.join("/tmp", filename)
    workbook.save(output_path)

    return send_from_directory(
        "/tmp",
        filename,
        as_attachment=True,
        download_name=filename
    )
@app.route("/export/financial-report")
def export_financial_report_excel():
    if "admin_id" not in session:
        return {"error": "Unauthorized"}, 401

    summary = get_finance_summary()

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Financial Report"

    sheet.append(["Class Finance — Financial Report"])
    sheet.append([])

    sheet.append(["Metric", "Value"])

    sheet.append([
        "Total Students",
        summary["total_students"]
    ])

    sheet.append([
        "Total Payment Records",
        summary["total_payments"]
    ])

    sheet.append([
        "Verified Amount (₦)",
        summary["verified_amount"]
    ])

    sheet.append([
        "Pending / Review Amount (₦)",
        summary["pending_amount"]
    ])

    sheet.append([
        "Flagged Amount (₦)",
        summary["flagged_amount"]
    ])

    sheet["A1"].font = Font(
        bold=True,
        size=16
    )

    for cell in sheet[3]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")

    sheet.column_dimensions["A"].width = 32
    sheet.column_dimensions["B"].width = 24

    filename = "financial_report.xlsx"

    output_path = os.path.join("/tmp", filename)
    workbook.save(output_path)

    return send_from_directory(
        "/tmp",
        filename,
        as_attachment=True,
        download_name=filename
    ) 
@app.route("/export/financial-report/pdf")
def export_financial_report_pdf():
    if "admin_id" not in session:
        return {"error": "Unauthorized"}, 401

    summary = get_finance_summary()

    filename = "financial_report.pdf"
    output_path = os.path.join("/tmp", filename)

    document = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()
    elements = []

    elements.append(
        Paragraph(
            "Class Finance — Financial Report",
            styles["Title"]
        )
    )

    elements.append(Spacer(1, 12))

    data = [
        ["Metric", "Value"],
        ["Total Students", str(summary["total_students"])],
        ["Total Payment Records", str(summary["total_payments"])],
        ["Verified Amount (₦)", f'{summary["verified_amount"]:,.2f}'],
        ["Pending / Review Amount (₦)", f'{summary["pending_amount"]:,.2f}'],
        ["Flagged Amount (₦)", f'{summary["flagged_amount"]:,.2f}']
    ]

    table = Table(
        data,
        colWidths=[260, 180]
    )

    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1565C0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ALIGN", (1, 1), (1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8)
        ])
    )

    elements.append(table)

    document.build(elements)

    return send_from_directory(
        "/tmp",
        filename,
        as_attachment=True,
        download_name=filename
    )

@app.route("/export/manual/<path:purpose>/pdf")
def export_manual_pdf(purpose):
    if "admin_id" not in session:
        return {"error": "Unauthorized"}, 401

    payment_list = generate_manual_purchase_list(purpose)

    filename = "manual_purchase_list.pdf"
    output_path = os.path.join("/tmp", filename)

    document = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    styles = getSampleStyleSheet()
    elements = []

    elements.append(
        Paragraph(
            f"Manual Purchase List — {purpose}",
            styles["Title"]
        )
    )

    elements.append(Spacer(1, 12))

    data = [
        ["S/N", "Matric Number", "Student Name", "Verified Paid (₦)"]
    ]

    for index, student in enumerate(payment_list, start=1):
        data.append([
            str(index),
            student["matric_number"],
            student["full_name"],
            f'{student["verified_paid"]:,.2f}'
        ])

    table = Table(
        data,
        colWidths=[35, 110, 190, 110]
    )

    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1565C0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (3, 1), (3, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
            ("TOPPADDING", (0, 0), (-1, 0), 8),
        ])
    )

    elements.append(table)

    document.build(elements)

    return send_from_directory(
        "/tmp",
        filename,
        as_attachment=True,
        download_name=filename
    )


# =========================================================
# OUTSTANDING PAYMENT EXPORTS
# =========================================================

@app.route("/export/outstanding/<path:purpose>")
def export_outstanding_excel(purpose):
    if "admin_id" not in session:
        return {"error": "Unauthorized"}, 401

    resolved = ai_reports.resolve_payment_purpose(purpose)

    if resolved.get("status") != "matched":
        return {"error": "Payment purpose not found."}, 404

    report = ai_reports.get_purpose_payment_report(
        resolved["purpose"]["id"]
    )

    if not report:
        return {"error": "Payment purpose not found."}, 404

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Outstanding Students"

    sheet.append([
        "S/N",
        "Matric Number",
        "Student Name",
        "Expected Amount (₦)",
        "Verified Paid (₦)",
        "Outstanding Balance (₦)"
    ])

    for index, student in enumerate(
        report["outstanding_students"], start=1
    ):
        sheet.append([
            index,
            student["matric_number"],
            student["full_name"],
            student["expected_amount"],
            student["verified_paid"],
            student["balance"]
        ])

    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")

    for column, width in {
        "A": 8,
        "B": 20,
        "C": 32,
        "D": 22,
        "E": 20,
        "F": 26,
    }.items():
        sheet.column_dimensions[column].width = width

    filename = "outstanding_students.xlsx"
    output_path = os.path.join("/tmp", filename)
    workbook.save(output_path)

    return send_from_directory(
        "/tmp",
        filename,
        as_attachment=True,
        download_name=filename
    )


@app.route("/export/outstanding/<path:purpose>/pdf")
def export_outstanding_pdf(purpose):
    if "admin_id" not in session:
        return {"error": "Unauthorized"}, 401

    resolved = ai_reports.resolve_payment_purpose(purpose)

    if resolved.get("status") != "matched":
        return {"error": "Payment purpose not found."}, 404

    report = ai_reports.get_purpose_payment_report(
        resolved["purpose"]["id"]
    )

    if not report:
        return {"error": "Payment purpose not found."}, 404

    filename = "outstanding_students.pdf"
    output_path = os.path.join("/tmp", filename)

    document = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=24,
        leftMargin=24,
        topMargin=30,
        bottomMargin=30
    )

    styles = getSampleStyleSheet()
    elements = []

    elements.append(
        Paragraph(
            f"Outstanding Students — {report['purpose']}",
            styles["Title"]
        )
    )

    elements.append(Spacer(1, 12))

    elements.append(
        Paragraph(
            f"Active students: {report['active_students']} | "
            f"Outstanding students: {report['outstanding_count']} | "
            f"Outstanding total: "
            f"₦{report['outstanding_total']:,.2f}",
            styles["Normal"]
        )
    )

    elements.append(Spacer(1, 12))

    data = [[
        "S/N",
        "Matric Number",
        "Student Name",
        "Expected (₦)",
        "Paid (₦)",
        "Balance (₦)"
    ]]

    for index, student in enumerate(
        report["outstanding_students"], start=1
    ):
        data.append([
            str(index),
            str(student["matric_number"]),
            str(student["full_name"]),
            f'{student["expected_amount"]:,.2f}',
            f'{student["verified_paid"]:,.2f}',
            f'{student["balance"]:,.2f}'
        ])

    table = Table(
        data,
        colWidths=[30, 85, 125, 75, 75, 75],
        repeatRows=1
    )

    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1565C0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
            ("TOPPADDING", (0, 0), (-1, 0), 8),
        ])
    )

    elements.append(table)
    document.build(elements)

    return send_from_directory(
        "/tmp",
        filename,
        as_attachment=True,
        download_name=filename
    )


@app.route('/api/ai', methods=['POST'])
def chat():
    data = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()

    if not question:
        return jsonify({
            "error": "Please enter a question."
        }), 400

    try:
        answer = ask_finance_ai(question)

        response_data = {"answer": answer}
        question_lower = question.casefold()

        purpose_result = ai_reports.resolve_payment_purpose(question)

        if purpose_result.get("status") == "matched":
            selected_purpose = purpose_result["purpose"]["name"]

            response_data["resolved_purpose"] = selected_purpose

            outstanding_intent = any(
                phrase in question_lower
                for phrase in (
                    "outstanding",
                    "owe",
                    "owing",
                    "unpaid",
                    "not paid",
                    "haven't paid",
                    "have not paid",
                )
            )

            paid_list_intent = any(
                phrase in question_lower
                for phrase in (
                    "who paid",
                    "who has paid",
                    "has paid",
                    "list paid",
                    "paid students",
                    "verified payers",
                    "purchase list",
                )
            )

            if outstanding_intent:
                response_data["export_outstanding_excel_url"] = url_for(
                    "export_outstanding_excel",
                    purpose=selected_purpose
                )

                response_data["export_outstanding_pdf_url"] = url_for(
                    "export_outstanding_pdf",
                    purpose=selected_purpose
                )

            elif paid_list_intent:
                response_data["export_manual_excel_url"] = url_for(
                    "export_manual_excel",
                    purpose=selected_purpose
                )

                response_data["export_manual_pdf_url"] = url_for(
                    "export_manual_pdf",
                    purpose=selected_purpose
                )

        financial_report_intent = any(
            phrase in question_lower
            for phrase in (
                "financial report",
                "finance report",
                "financial summary",
                "generate report",
            )
        )

        if financial_report_intent:
            response_data["export_financial_excel_url"] = url_for(
                "export_financial_report_excel"
            )

            response_data["export_financial_pdf_url"] = url_for(
                "export_financial_report_pdf"
            )

        return jsonify(response_data)

    except Exception:
        app.logger.exception("AI request failed")
        return jsonify({
            "error": "The finance report could not be generated. Please try again."
        }), 500


@app.route("/dashboard")
def home():

    access = admin_required()

    if access:
        return access

    connection = get_connection()

    total_students = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM students
        """
    ).fetchone()["count"]

    active_students = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM students
        WHERE status = 'Active'
        """
    ).fetchone()["count"]

    payment_announcement = connection.execute(
        """
        SELECT
            id,
            title,
            message,
            payment_purpose,
            start_date,
            deadline_date,
            status,
            priority,
            created_at
        FROM announcements
        WHERE status = 'Active'
          AND payment_purpose IS NOT NULL
        ORDER BY priority DESC, created_at DESC
        LIMIT 1
        """
    ).fetchone()

    latest_announcements = connection.execute(
        """
        SELECT
            id,
            title,
            message,
            payment_purpose,
            start_date,
            deadline_date,
            status,
            priority,
            created_at
        FROM announcements
        WHERE status = 'Active'
        ORDER BY priority DESC, created_at DESC
        LIMIT 5
        """
    ).fetchall()

    advertisements = connection.execute(
        """
        SELECT
            id,
            title,
            description,
            image_url,
            target_url,
            start_date,
            end_date,
            status,
            priority,
            created_at
        FROM advertisements
        WHERE status = 'Active'
        ORDER BY priority DESC, created_at DESC
        LIMIT 3
        """
    ).fetchall()

    connection.close()

    return render_template(
        "dashboard.html",
        total_students=total_students,
        active_students=active_students,
        payment_announcement=payment_announcement,
        latest_announcements=latest_announcements,
        advertisements=advertisements
    )
@app.route("/")
def landing():
    return render_template("landing.html")
# =========================================================
# STUDENTS
# =========================================================

@app.route("/student")
def student_portal():

    connection = get_connection()

    payment_announcement = connection.execute(
        """
        SELECT
            id,
            title,
            message,
            payment_purpose,
            start_date,
            deadline_date,
            status,
            priority,
            created_at
        FROM announcements
        WHERE status = 'Active'
          AND payment_purpose IS NOT NULL
        ORDER BY priority DESC, created_at DESC
        LIMIT 1
        """
    ).fetchone()

    latest_announcements = connection.execute(
        """
        SELECT
            id,
            title,
            message,
            payment_purpose,
            start_date,
            deadline_date,
            status,
            priority,
            created_at
        FROM announcements
        WHERE status = 'Active'
        ORDER BY priority DESC, created_at DESC
        LIMIT 5
        """
    ).fetchall()

    advertisements = connection.execute(
        """
        SELECT
            id,
            title,
            description,
            image_url,
            target_url,
            start_date,
            end_date,
            status,
            priority,
            created_at
        FROM advertisements
        WHERE status = 'Active'
        ORDER BY priority DESC, created_at DESC
        LIMIT 3
        """
    ).fetchall()

    connection.close()

    return render_template(
        "student_portal.html",
        payment_announcement=payment_announcement,
        latest_announcements=latest_announcements,
        advertisements=advertisements
    )
@app.route(
    "/student/payment",
    methods=["GET", "POST"]
)
def student_payment():

    connection = get_connection()

    purposes = connection.execute(
        """
        SELECT name, expected_amount
        FROM payment_purposes
        WHERE status = 'Active'
        ORDER BY name
        """
    ).fetchall()

    if request.method == "POST":

        matric_number = request.form.get(
            "matric_number",
            ""
        ).strip()

        full_name = request.form.get(
            "full_name",
            ""
        ).strip()

        amount = request.form.get(
            "amount",
            ""
        ).strip()

        purpose = request.form.get(
            "purpose",
            ""
        ).strip()

        # Validate against official active payment purposes
        selected_purpose = connection.execute(
            """
            SELECT name, expected_amount
            FROM payment_purposes
            WHERE name = ?
              AND status = 'Active'
            """,
            (purpose,)
        ).fetchone()

        if not selected_purpose:
            connection.close()
            return (
                "Invalid payment purpose. Please select an official active purpose.",
                400
            )

        purpose = selected_purpose["name"]

        receipt_reference = request.form.get(
            "receipt_reference",
            ""
        ).strip()

        receipt = request.files.get(
            "receipt"
        )

        # ================================================
        # VERIFY STUDENT
        # ================================================

        student = connection.execute(
            """
            SELECT id, full_name
            FROM students
            WHERE matric_number = ?
              AND LOWER(full_name) = LOWER(?)
              AND status = 'Active'
            """,
            (
                matric_number,
                full_name
            )
        ).fetchone()

        if not student:

            connection.close()

            return (
                "Student details could not be verified. "
                "Please check your matric number and full name.",
                400
            )

        # ================================================
        # AMOUNT VALIDATION
        # ================================================

        try:

            amount_value = float(amount)

            if not math.isfinite(amount_value) or amount_value <= 0:

                connection.close()

                return (
                    "Invalid payment amount.",
                    400
                )

        except (ValueError, TypeError):

            connection.close()

            return (
                "Invalid payment amount.",
                400
            )

        # ================================================
        # RECEIPT VALIDATION
        # ================================================

        if not receipt or not receipt.filename:

            connection.close()

            return (
                "Please upload your payment receipt.",
                400
            )

        if not allowed_file(receipt.filename):

            connection.close()

            return (
                "Invalid receipt file type. "
                "Only JPG, JPEG, PNG and PDF are allowed.",
                400
            )

        # ================================================
        # CREATE UNIQUE RECEIPT FILE
        # ================================================

        clean_filename = secure_filename(
            receipt.filename
        )

        extension = clean_filename.rsplit(
            ".",
            1
        )[1].lower()

        receipt_filename = (
            f"{uuid.uuid4().hex}.{extension}"
        )

        os.makedirs(
            UPLOAD_FOLDER,
            exist_ok=True
        )

        receipt_path = os.path.join(
            UPLOAD_FOLDER,
            receipt_filename
        )

        commit_started = False
        commit_completed = False

        try:
            receipt.save(receipt_path)

            # ================================================
            # UPLOAD RECEIPT TO CLOUDINARY
            # ================================================

            cloudinary_result = cloudinary.uploader.upload(
                receipt_path,
                folder="class_finance/receipts",
                resource_type="auto"
            )

            receipt_filename = cloudinary_result["secure_url"]

            # ================================================
            # OCR
            # ================================================

            ocr_text = extract_receipt_ocr(
                receipt_path
            )

            ocr_fields = extract_receipt_fields(
                ocr_text
            )

            # ================================================
            # PAYMENT VERIFICATION
            # ================================================

            verification_result = verify_payment_with_ocr(
                submitted_amount=amount_value,
                student_name=student["full_name"],
                purpose=purpose,
                ocr_fields=ocr_fields
            )

            ai_verification = verification_result[
                "verification"
            ]

            verification_note = verification_result[
                "note"
            ]

            # ================================================
            # CHECK DUPLICATE REFERENCE
            # ================================================

            duplicate = None

            if receipt_reference:

                duplicate = connection.execute(
                    """
                    SELECT id
                    FROM payments
                    WHERE receipt_reference = ?
                    """,
                    (receipt_reference,)
                ).fetchone()

            # ================================================
            # FINAL STATUS
            # ================================================

            if duplicate:

                status = "Flagged"

                verification_note = (
                    "Possible duplicate receipt reference. "
                    + verification_note
                )

            elif ai_verification == "Review":

                status = "Review"

            elif ai_verification == "Flagged":

                status = "Flagged"

            else:

                # AI/OCR cannot approve payments.
                # Even an AI result of Verified requires admin approval.
                status = "Pending"

            # ================================================
            # SAVE PAYMENT
            # ================================================

            connection.execute(
                """
                INSERT INTO payments (
                    student_id,
                    amount,
                    purpose,
                    receipt_reference,
                    receipt_file,
                    status,
                    verification_note,
                    ocr_text,
                    ocr_amount,
                    ocr_date,
                    ocr_reference,
                    ocr_sender,
                    ocr_recipient,
                    ocr_narration,
                    ai_verification,
                    ai_verification_note
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    student["id"],
                    amount_value,
                    purpose,
                    receipt_reference,
                    receipt_filename,
                    status,
                    verification_note,
                    ocr_text,
                    ocr_fields["ocr_amount"],
                    ocr_fields["ocr_date"],
                    ocr_fields["ocr_reference"],
                    ocr_fields["ocr_sender"],
                    ocr_fields["ocr_recipient"],
                    ocr_fields["ocr_narration"],
                    ai_verification,
                    verification_note
                )
            )

            commit_started = True
            connection.commit()
            commit_completed = True

        except Exception:
            app.logger.exception(
                "Student payment receipt submission failed."
            )

            if not commit_completed:
                try:
                    connection.rollback()
                except Exception:
                    app.logger.exception(
                        "Payment submission rollback failed."
                    )

            try:
                connection.close()
            except Exception:
                app.logger.exception(
                    "Payment database connection cleanup failed."
                )

            try:
                if os.path.exists(receipt_path):
                    os.remove(receipt_path)
            except OSError:
                app.logger.exception(
                    "Temporary receipt cleanup failed."
                )

            if commit_started:
                error_message = (
                    "We could not confirm whether your submission was saved. "
                    "Please do not submit again yet. Contact your class "
                    "representative to check your payment history."
                )
            else:
                error_message = (
                    "Your upload could not be completed. "
                    "Please try again."
                )

            return render_template(
                "student_payment.html",
                purposes=purposes,
                error_message=error_message
            )

        try:
            connection.close()
        except Exception:
            app.logger.exception(
                "Payment connection cleanup failed after commit."
            )

        try:
            if os.path.exists(receipt_path):
                os.remove(receipt_path)
        except OSError:
            app.logger.exception(
                "Temporary receipt cleanup failed after commit."
            )

        return render_template("student_success.html")



    connection.close()

    return render_template(
        "student_payment.html",
        purposes=purposes
    )
@app.route("/student/history")
def student_history():

    matric_number = request.args.get(
        "matric_number",
        ""
    ).strip()

    connection = get_connection()

    student = None
    payments = []

    if matric_number:

        student = connection.execute(
            """
            SELECT
                id,
                matric_number,
                full_name
            FROM students
            WHERE matric_number = ?
              AND status = 'Active'
            """,
            (matric_number,)
        ).fetchone()

        if student:

            payments = connection.execute(
                """
                SELECT
                    amount,
                    purpose,
                    payment_date,
                    status
                FROM payments
                WHERE student_id = ?
                ORDER BY payment_date DESC
                """,
                (student["id"],)
            ).fetchall()

    connection.close()

    return render_template(
        "student_history.html",
        student=student,
        payments=payments,
        matric_number=matric_number
    )


@app.route("/students")
def students():
    access = admin_required()
    if access:
        return access

    search = request.args.get(
        "search",
        ""
    ).strip()

    connection = get_connection()

    if search:

        student_list = connection.execute(
            """
            SELECT *
            FROM students
            WHERE full_name LIKE ?
               OR matric_number LIKE ?
            ORDER BY full_name
            """,
            (
                f"%{search}%",
                f"%{search}%"
            )
        ).fetchall()

    else:

        student_list = connection.execute(
            """
            SELECT *
            FROM students
            ORDER BY full_name
            """
        ).fetchall()

    connection.close()

    message = request.args.get(
        "message",
        ""
    ).strip()

    return render_template(
        "students.html",
        students=student_list,
        search=search,
        message=message
    )

# =========================================================
# ADD STUDENT
# =========================================================

@app.route("/students/add", methods=["POST"])
def add_student():

    access = admin_required()

    if access:
        return access

    matric_number = request.form.get(
        "matric_number",
        ""
    ).strip().upper()

    full_name = request.form.get(
        "full_name",
        ""
    ).strip().upper()

    if not matric_number or not full_name:
        return redirect(url_for("students"))

    connection = get_connection()

    try:
        connection.execute(
            """
            INSERT INTO students
            (matric_number, full_name, status)
            VALUES (?, ?, 'Active')
            """,
            (
                matric_number,
                full_name
            )
        )

        connection.commit()

    except (sqlite3.IntegrityError, PostgreSQLIntegrityError):
        connection.close()

        return redirect(
            url_for(
                "students",
                message="This matric number already exists."
            )
        )

    connection.close()

    return redirect(url_for("students"))
# =========================================================
# ADD PAYMENT
# =========================================================

@app.route(
    "/payments/add",
    methods=["GET", "POST"]
)
def add_payment():

    access = admin_required()

    if access:
        return access

    connection = get_connection()

    students = connection.execute(
        """
        SELECT
            id,
            matric_number,
            full_name
        FROM students
        WHERE status = 'Active'
        ORDER BY full_name
        """
    ).fetchall()

    if request.method == "POST":

        student_id = request.form.get(
            "student_id"
        )

        amount = request.form.get(
            "amount",
            ""
        ).strip()

        purpose = request.form.get(
            "purpose",
            ""
        ).strip()

        receipt_reference = request.form.get(
            "receipt_reference",
            ""
        ).strip()

        receipt = request.files.get(
            "receipt"
        )

        flags = []
        ocr_text = ""


        # =================================================
        # AMOUNT VALIDATION
        # =================================================

        try:

            amount_value = float(amount)

            if amount_value <= 0:

                flags.append(
                    "Invalid amount"
                )

        except (ValueError, TypeError):

            flags.append(
                "Invalid amount"
            )

            amount_value = 0


        # =================================================
        # PURPOSE VALIDATION
        # =================================================

        if not purpose:

            flags.append(
                "Missing payment purpose"
            )


        # =================================================
        # STUDENT VALIDATION
        # =================================================

        student = connection.execute(
            """
            SELECT id
            FROM students
            WHERE id = ?
            """,
            (student_id,)
        ).fetchone()

        if not student:

            flags.append(
                "Student not found"
            )


        # =================================================
        # RECEIPT VALIDATION
        # =================================================

        receipt_filename = ""

        if receipt and receipt.filename:

            if not allowed_file(
                receipt.filename
            ):

                flags.append(
                    "Invalid receipt file type. "
                    "Only JPG, JPEG, PNG and PDF are allowed."
                )

            else:

                clean_filename = secure_filename(
                    receipt.filename
                )

                extension = clean_filename.rsplit(
                    ".",
                    1
                )[1].lower()

                # Unique filename
                receipt_filename = (
                    f"{uuid.uuid4().hex}.{extension}"
                )

        else:

            flags.append(
                "Missing receipt"
            )


        # =================================================
        # DUPLICATE RECEIPT REFERENCE
        # =================================================

        if receipt_reference:

            duplicate = connection.execute(
                """
                SELECT id
                FROM payments
                WHERE receipt_reference = ?
                """,
                (receipt_reference,)
            ).fetchone()

            if duplicate:

                flags.append(
                    "Possible duplicate receipt reference"
                )


        # =================================================
        # DETERMINE PAYMENT STATUS
        # =================================================

        if flags:

            status = "Flagged"

        else:

            status = "Pending"


        # =================================================
        # CREATE RECEIPT DIRECTORY
        # =================================================

        os.makedirs(
            UPLOAD_FOLDER,
            exist_ok=True
        )

        # =================================================
        # SAVE RECEIPT
        # =================================================

        if receipt_filename:

            receipt_path = os.path.join(
                UPLOAD_FOLDER,
                receipt_filename
            )

            receipt.save(receipt_path)

            # =================================================
            # UPLOAD RECEIPT TO CLOUDINARY
            # =================================================

            cloudinary_result = cloudinary.uploader.upload(
                receipt_path,
                folder="class_finance/receipts",
                resource_type="auto"
            )

            receipt_filename = cloudinary_result["secure_url"]

            # =================================================
            # PHASE 4 — OCR PROCESSING
            # =================================================

            ocr_text = extract_receipt_ocr(
                receipt_path
            )

            ocr_fields = extract_receipt_fields(
                ocr_text
            )

        else:

            ocr_text = ""

            ocr_fields = {
                "ocr_amount": None,
                "ocr_date": None,
                "ocr_reference": None,
                "ocr_sender": None,
                "ocr_recipient": None,
                "ocr_narration": None
            }
        # =================================================
        # PHASE 4.3 — AI/OCR PAYMENT VERIFICATION
        # =================================================

        if student:

            student_details = connection.execute(
                """
                SELECT full_name
                FROM students
                WHERE id = ?
                """,
                (student_id,)
            ).fetchone()

            if student_details:

                verification_result = verify_payment_with_ocr(
                    submitted_amount=amount_value,
                    student_name=student_details["full_name"],
                    purpose=purpose,
                    ocr_fields=ocr_fields
                )

                ai_verification = verification_result[
                    "verification"
                ]

                verification_note = verification_result[
                    "note"
                ]

            else:

                ai_verification = "Review"

                verification_note = (
                    "Student details could not be retrieved."
                )

        else:

            ai_verification = "Flagged"

            verification_note = (
                "Payment could not be linked to a valid student."
            )

        # =================================================
        # FINAL PAYMENT STATUS
        # =================================================

        if flags or ai_verification == "Flagged":

            status = "Flagged"

        elif ai_verification == "Review":

            status = "Review"

        else:

            # Admin-added records also require explicit verification.
            status = "Pending"

        # =================================================
        # SAVE PAYMENT
        # =================================================

        connection.execute(
            """
            INSERT INTO payments (
                student_id,
                amount,
                purpose,
                receipt_reference,
                receipt_file,
                status,
                verification_note,
                ocr_text,
                ocr_amount,
                ocr_date,
                ocr_reference,
                ocr_sender,
                ocr_recipient,
                ocr_narration,
                ai_verification,
                ai_verification_note
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                student_id,
                amount_value,
                purpose,
                receipt_reference,
                receipt_filename,
                status,
                verification_note,
                ocr_text,
                ocr_fields["ocr_amount"],
                ocr_fields["ocr_date"],
                ocr_fields["ocr_reference"],
                ocr_fields["ocr_sender"],
                ocr_fields["ocr_recipient"],
                ocr_fields["ocr_narration"],
                ai_verification,
                verification_note
            )
        )
        connection.commit()

        connection.close()

        return redirect(
            url_for("students")
        )

    connection.close()

    return render_template(
        "add_payment.html",
        students=students
    )

# =========================================================
# VERIFY PAYMENT
# =========================================================

@app.route(
    "/payments/<int:payment_id>/verify",
    methods=["POST"]
)
def verify_payment(payment_id):

    access = admin_required()

    if access:
        return access

    connection = get_connection()

    payment = connection.execute(
        """
        SELECT
            id,
            status
        FROM payments
        WHERE id = ?
        """,
        (payment_id,)
    ).fetchone()

    if not payment:
        connection.close()

        return redirect(
            url_for(
                "students",
                message="Payment not found."
            )
        )

    if payment["status"] == "Verified":
        connection.close()

        return redirect(
            url_for(
                "students",
                message="Payment is already verified."
            )
        )

    if payment["status"] == "Voided":
        connection.close()

        return redirect(
            url_for(
                "finance_dashboard",
                message="This payment has been voided and cannot be verified."
            )
        )

    # -----------------------------------------------
    # Mark payment as officially verified
    # -----------------------------------------------

    admin_username = session.get(
        "admin_username",
        "Admin"
    )

    connection.execute(
        """
        UPDATE payments
        SET
            status = 'Verified',
            verified_by = ?,
            verified_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            admin_username,
            payment_id
        )
    )

    connection.commit()
    connection.close()

    return redirect(
        url_for(
            "finance_dashboard",
            message="Payment verified successfully."
        )
    )
# =========================================================
# VOID PAYMENT
# =========================================================

@app.route(
    "/payments/<int:payment_id>/void",
    methods=["POST"]
)
def void_payment(payment_id):

    access = admin_required()

    if access:
        return access

    reason = request.form.get("reason", "").strip()

    if not reason:
        return redirect(
            url_for(
                "finance_dashboard",
                message="A reason is required to void a payment."
            )
        )

    connection = get_connection()

    payment = connection.execute(
        """
        SELECT id, status, verification_note
        FROM payments
        WHERE id = ?
        """,
        (payment_id,)
    ).fetchone()

    if not payment:
        connection.close()
        return redirect(
            url_for("finance_dashboard", message="Payment not found.")
        )

    if payment["status"] == "Voided":
        connection.close()
        return redirect(
            url_for("finance_dashboard", message="Payment is already voided.")
        )

    admin_username = session.get("admin_username", "Admin")

    from datetime import datetime, timezone

    voided_at = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )

    audit_note = (
        f"VOIDED by {admin_username} at {voided_at}. "
        f"Reason: {reason}"
    )

    previous_note = payment["verification_note"] or ""
    updated_note = (
        f"{previous_note}\n{audit_note}"
        if previous_note else audit_note
    )

    connection.execute(
        """
        UPDATE payments
        SET status = 'Voided',
            verification_note = ?
        WHERE id = ?
          AND status != 'Voided'
        """,
        (updated_note, payment_id)
    )

    connection.commit()
    connection.close()

    return redirect(
        url_for(
            "finance_dashboard",
            message="Payment voided successfully. The record was preserved."
        )
    )


# =========================================================
# FINANCIAL DASHBOARD
# =========================================================

@app.route("/finance")
def finance_dashboard():

    access = admin_required()

    if access:
        return access

    connection = get_connection()


    # =====================================================
    # TOTAL RECEIVED
    # =====================================================

    total_received = connection.execute(
        """
        SELECT COALESCE(
            SUM(amount),
            0
        ) AS total_received
        FROM payments
        WHERE status = 'Verified'
        """
    ).fetchone()["total_received"]


    # =====================================================
    # NUMBER OF ACTIVE STUDENTS
    # =====================================================

    active_students = connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM students
        WHERE status = 'Active'
        """
    ).fetchone()["count"]


    # =====================================================
    # TOTAL EXPECTED
    #
    # Every active student is expected to pay the amount
    # defined for each active payment purpose.
    # =====================================================

    total_expected = connection.execute(
        """
        SELECT COALESCE(
            SUM(
                ? * expected_amount
            ),
            0
        ) AS total_expected
        FROM payment_purposes
        WHERE status = 'Active'
        """,
        (active_students,)
    ).fetchone()["total_expected"]


    # =====================================================
    # OUTSTANDING
    # =====================================================

    outstanding = (
        total_expected
        - total_received
    )

    if outstanding < 0:

        outstanding = 0


    # =====================================================
    # PENDING TOTAL
    # =====================================================

    pending = connection.execute(
        """
        SELECT COALESCE(
            SUM(amount),
            0
        ) AS pending
        FROM payments
        WHERE status IN ('Pending', 'Review')
        """
    ).fetchone()["pending"]


    # =====================================================
    # VERIFIED TOTAL
    # =====================================================

    verified = connection.execute(
        """
        SELECT COALESCE(
            SUM(amount),
            0
        ) AS verified
        FROM payments
        WHERE status = 'Verified'
        """
    ).fetchone()["verified"]


    # =====================================================
    # FLAGGED TOTAL
    # =====================================================

    flagged = connection.execute(
        """
        SELECT COALESCE(
            SUM(amount),
            0
        ) AS flagged
        FROM payments
        WHERE status = 'Flagged'
        """
    ).fetchone()["flagged"]


    # =====================================================
    # PAYMENT PURPOSE BREAKDOWN
    # =====================================================

    purposes = connection.execute(
        """
        SELECT

            pp.id,

            pp.name,

            pp.expected_amount,
            (
                ? * pp.expected_amount
            ) AS expected_total,

            COALESCE(
                (
                    SELECT SUM(
                        p.amount
                    )
                    FROM payments p
                    WHERE p.status = 'Verified'
                      AND (
                          p.purpose = pp.name
                          OR p.purpose IN (
                              SELECT alias_name
                              FROM payment_purpose_aliases
                              WHERE purpose_id = pp.id
                          )
                      )
                ),
                0
            ) AS received_total

        FROM payment_purposes pp

        WHERE pp.status = 'Active'

        ORDER BY pp.name
        """,
        (active_students,)
    ).fetchall()

    # =====================================================
    # PAYMENT REVIEW LIST
    # =====================================================

    payments = connection.execute(
        """
        SELECT
            p.id,
            p.amount,
            p.purpose,
            p.payment_date,
            p.receipt_reference,
            p.receipt_file,
            p.status,
            p.verification_note,
            p.ai_verification,
            s.full_name,
            s.matric_number
        FROM payments p
        JOIN students s
            ON s.id = p.student_id
        ORDER BY p.payment_date DESC
        """
    ).fetchall()

    connection.close()


    return render_template(
        "finance.html",

        total_expected=total_expected,

        total_received=total_received,

        outstanding=outstanding,

        pending=pending,

        verified=verified,

        flagged=flagged,
        purposes=purposes,
        payments=payments,
    )


# =========================================================
# PAYMENT PURPOSE MANAGEMENT
# =========================================================

@app.route(
    "/payment-purposes",
    methods=["GET", "POST"]
)
def payment_purposes():

    access = admin_required()

    if access:
        return access

    connection = get_connection()

    if request.method == "POST":

        action = request.form.get("action", "add").strip()

        try:

            # -------------------------------------------
            # ADD PURPOSE
            # -------------------------------------------

            if action == "add":

                name = request.form.get("name", "").strip()
                expected_amount = request.form.get(
                    "expected_amount", ""
                ).strip()

                amount = float(expected_amount)

                if not name:
                    raise ValueError("Payment purpose is required.")

                if not amount > 0 or amount == float("inf"):
                    raise ValueError(
                        "Expected amount must be greater than zero."
                    )

                alias_conflict = connection.execute(
                    """
                    SELECT purpose_id
                    FROM payment_purpose_aliases
                    WHERE alias_name = ?
                    """,
                    (name,)
                ).fetchone()

                if alias_conflict:
                    raise ValueError(
                        "This name is reserved by an existing purpose's history."
                    )

                connection.execute(
                    """
                    INSERT INTO payment_purposes (
                        name,
                        expected_amount
                    )
                    VALUES (?, ?)
                    """,
                    (name, amount)
                )

                purpose = connection.execute(
                    """
                    SELECT id
                    FROM payment_purposes
                    WHERE name = ?
                    """,
                    (name,)
                ).fetchone()

                connection.execute(
                    """
                    INSERT INTO payment_purpose_aliases (
                        purpose_id,
                        alias_name
                    )
                    VALUES (?, ?)
                    ON CONFLICT (alias_name) DO NOTHING
                    """,
                    (purpose["id"], name)
                )

                connection.commit()

                flash(
                    "Payment purpose added successfully.",
                    "success"
                )

            # -------------------------------------------
            # EDIT PURPOSE
            # -------------------------------------------

            elif action == "edit":

                purpose_id = request.form.get("purpose_id", "").strip()
                name = request.form.get("name", "").strip()
                expected_amount = request.form.get(
                    "expected_amount", ""
                ).strip()

                if not purpose_id.isdigit():
                    raise ValueError("Invalid payment purpose.")

                if not name:
                    raise ValueError("Payment purpose is required.")

                amount = float(expected_amount)

                if not amount > 0 or amount == float("inf"):
                    raise ValueError(
                        "Expected amount must be greater than zero."
                    )

                existing = connection.execute(
                    """
                    SELECT id, name
                    FROM payment_purposes
                    WHERE id = ?
                    """,
                    (int(purpose_id),)
                ).fetchone()

                if not existing:
                    raise ValueError("Payment purpose not found.")

                alias_conflict = connection.execute(
                    """
                    SELECT purpose_id
                    FROM payment_purpose_aliases
                    WHERE alias_name = ?
                      AND purpose_id != ?
                    """,
                    (name, int(purpose_id))
                ).fetchone()

                if alias_conflict:
                    raise ValueError(
                        "That name belongs to another purpose's history."
                    )

                duplicate = connection.execute(
                    """
                    SELECT id
                    FROM payment_purposes
                    WHERE name = ?
                      AND id != ?
                    """,
                    (name, int(purpose_id))
                ).fetchone()

                if duplicate:
                    raise ValueError(
                        "Another payment purpose already uses that name."
                    )

                # Preserve the old name before changing it.
                connection.execute(
                    """
                    INSERT INTO payment_purpose_aliases (
                        purpose_id,
                        alias_name
                    )
                    VALUES (?, ?)
                    ON CONFLICT (alias_name) DO NOTHING
                    """,
                    (int(purpose_id), existing["name"])
                )

                connection.execute(
                    """
                    UPDATE payment_purposes
                    SET
                        name = ?,
                        expected_amount = ?
                    WHERE id = ?
                    """,
                    (name, amount, int(purpose_id))
                )

                # Register the new name as well.
                connection.execute(
                    """
                    INSERT INTO payment_purpose_aliases (
                        purpose_id,
                        alias_name
                    )
                    VALUES (?, ?)
                    ON CONFLICT (alias_name) DO NOTHING
                    """,
                    (int(purpose_id), name)
                )

                connection.commit()

                flash(
                    "Payment purpose updated. Historical payment records were preserved.",
                    "success"
                )

            # -------------------------------------------
            # ACTIVATE / DEACTIVATE
            # -------------------------------------------

            elif action == "toggle":

                purpose_id = request.form.get("purpose_id", "").strip()

                if not purpose_id.isdigit():
                    raise ValueError("Invalid payment purpose.")

                existing = connection.execute(
                    """
                    SELECT id, name, status
                    FROM payment_purposes
                    WHERE id = ?
                    """,
                    (int(purpose_id),)
                ).fetchone()

                if not existing:
                    raise ValueError("Payment purpose not found.")

                new_status = (
                    "Inactive"
                    if existing["status"] == "Active"
                    else "Active"
                )

                connection.execute(
                    """
                    UPDATE payment_purposes
                    SET status = ?
                    WHERE id = ?
                    """,
                    (new_status, int(purpose_id))
                )

                connection.commit()

                flash(
                    f"{existing['name']} is now {new_status.lower()}.",
                    "success"
                )

            else:
                raise ValueError("Invalid management action.")

        except (ValueError, TypeError) as error:

            connection.rollback()
            flash(str(error), "error")

        except Exception:

            connection.rollback()
            flash(
                "The operation could not be completed. Please check the purpose and try again.",
                "error"
            )

    purposes = connection.execute(
        """
        SELECT *
        FROM payment_purposes
        ORDER BY name
        """
    ).fetchall()

    connection.close()

    return render_template(
        "payment_purposes.html",
        purposes=purposes
    )


# STUDENT FINANCIAL RECORD
# =========================================================

@app.route("/students/<int:student_id>/finance")
def student_finance(student_id):

    access = admin_required()

    if access:
        return access

    connection = get_connection()

    # -----------------------------------------------------
    # Get student
    # ------------------------------------------------------
    student = connection.execute(
        """
        SELECT *
        FROM students
        WHERE id = ?
        """,
        (student_id,)
    ).fetchone()

    if not student:

        connection.close()

        return "Student not found", 404


    # -----------------------------------------------------
    # Get student's payment history
    # -----------------------------------------------------

    payments = connection.execute(
        """
        SELECT
            p.*,
            s.full_name,
            s.matric_number

        FROM payments p

        JOIN students s
            ON s.id = p.student_id

        WHERE p.student_id = ?

        ORDER BY p.payment_date DESC
        """,
        (student_id,)
    ).fetchall()


    # -----------------------------------------------------
    # Total amount paid
    # -----------------------------------------------------

    total_paid = connection.execute(
        """
        SELECT COALESCE(
            SUM(amount),
            0
        )

        FROM payments

        WHERE student_id = ?
        """,
        (student_id,)
    ).fetchone()["coalesce"]


    # -----------------------------------------------------
    # Number of payments
    # -----------------------------------------------------

    payment_count = connection.execute(
        """
        SELECT COUNT(*)
        FROM payments
        WHERE student_id = ?
        """,
        (student_id,)
    ).fetchone()["count"]


    connection.close()


    return render_template(
        "student_finance.html",

        student=student,

        payments=payments,

        total_paid=total_paid,

        payment_count=payment_count
    )


# =========================================================
# VIEW RECEIPT
# =========================================================

@app.route(
    "/uploads/receipts/<filename>"
)
def view_receipt(filename):

    access = admin_required()

    if access:
        return access

    if filename.startswith("http://") or filename.startswith("https://"):
        return redirect(filename)

    return send_from_directory(
        UPLOAD_FOLDER,
        filename,
        as_attachment=False
    )


# =========================================================
# DOWNLOAD RECEIPT
# =========================================================

@app.route(
    "/uploads/receipts/<filename>/download"
)
def download_receipt(filename):

    access = admin_required()

    if access:
        return access

    if filename.startswith("http://") or filename.startswith("https://"):
        return redirect(filename)

    return send_from_directory(
        UPLOAD_FOLDER,
        filename,
        as_attachment=True
    )


# =========================================================
# RUN APPLICATION
# =========================================================

if __name__ == "__main__":

        app.run(debug=True)
