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
from PIL import Image
import pytesseract
import os
import re
import uuid
import sqlite3
from psycopg2 import IntegrityError as PostgreSQLIntegrityError


# =========================================================
# APP CONFIGURATION
# =========================================================

app = Flask(__name__)
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

@app.route("/")
def home():

    access = admin_required()

    if access:
        return access

    connection = get_connection()

    total_students = connection.execute(
        """
        SELECT COUNT(*)
        FROM students
        """
    ).fetchone()[0]

    active_students = connection.execute(
        """
        SELECT COUNT(*)
        FROM students
        WHERE status = 'Active'
        """
    ).fetchone()[0]

    connection.close()

    return render_template(
        "dashboard.html",
        total_students=total_students,
        active_students=active_students
    )

# =========================================================
# STUDENTS
# =========================================================

@app.route("/student")
def student_portal():
    return render_template("student_portal.html")
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

            if amount_value <= 0:

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

        receipt.save(receipt_path)

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

        elif ai_verification == "Verified":

            status = "Verified"

        elif ai_verification == "Review":

            status = "Review"

        else:

            status = "Flagged"

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

        connection.commit()
        connection.close()

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

        # =================================================
        # FINAL PAYMENT STATUS
        # =================================================

        if ai_verification == "Verified" and not flags:

            status = "Verified"

        elif ai_verification == "Review":

            status = "Review"

        elif ai_verification == "Flagged":

            status = "Flagged"

        elif flags:

            status = "Flagged"

        else:

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
        )
        FROM payments
        """
    ).fetchone()[0]


    # =====================================================
    # NUMBER OF ACTIVE STUDENTS
    # =====================================================

    active_students = connection.execute(
        """
        SELECT COUNT(*)
        FROM students
        WHERE status = 'Active'
        """
    ).fetchone()[0]


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
        )
        FROM payment_purposes
        WHERE status = 'Active'
        """,
        (active_students,)
    ).fetchone()[0]


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
        )
        FROM payments
        WHERE status = 'Pending'
        """
    ).fetchone()[0]


    # =====================================================
    # VERIFIED TOTAL
    # =====================================================

    verified = connection.execute(
        """
        SELECT COALESCE(
            SUM(amount),
            0
        )
        FROM payments
        WHERE status = 'Verified'
        """
    ).fetchone()[0]


    # =====================================================
    # FLAGGED TOTAL
    # =====================================================

    flagged = connection.execute(
        """
        SELECT COALESCE(
            SUM(amount),
            0
        )
        FROM payments
        WHERE status = 'Flagged'
        """
    ).fetchone()[0]


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
                    WHERE p.purpose = pp.name
                ),
                0
            ) AS received_total

        FROM payment_purposes pp

        WHERE pp.status = 'Active'

        ORDER BY pp.name
        """,
        (active_students,)
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

        purposes=purposes
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

        name = request.form.get(
            "name",
            ""
        ).strip()

        expected_amount = request.form.get(
            "expected_amount",
            ""
        ).strip()


        # -----------------------------------------------
        # VALIDATE PURPOSE
        # -----------------------------------------------

        if not name:

            flash(
                "Payment purpose is required.",
                "error"
            )

        else:

            try:

                amount = float(
                    expected_amount
                )

                if amount <= 0:

                    flash(
                        "Expected amount must be greater than zero.",
                        "error"
                    )

                else:

                    connection.execute(
                        """
                        INSERT INTO payment_purposes (
                            name,
                            expected_amount
                        )
                        VALUES (?, ?)
                        """,
                        (
                            name,
                            amount
                        )
                    )

                    connection.commit()

                    flash(
                        "Payment purpose added successfully.",
                        "success"
                    )

            except (
                ValueError,
                TypeError
            ):

                flash(
                    "Enter a valid expected amount.",
                    "error"
                )

            except Exception:

                flash(
                    "That payment purpose already exists.",
                    "error"
                )


    # =====================================================
    # LOAD PURPOSES
    # =====================================================

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


# =========================================================
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
    ).fetchone()[0]


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
    ).fetchone()[0]


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

    return send_from_directory(
        UPLOAD_FOLDER,
        filename,
        as_attachment=True
    )


# =========================================================
# RUN APPLICATION
# =========================================================

if __name__ == "__main__":

        app.run(debug=False)
