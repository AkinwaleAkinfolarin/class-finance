"""
Read-only reporting functions for BME Class '29 Finance System.

All financial calculations are performed from database records.
This module does not create, update, delete, verify or void payments.
"""

import re
from database import get_connection


PAYMENT_STATUSES = (
    "Verified",
    "Pending",
    "Review",
    "Flagged",
    "Voided",
)


def _normalise_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def get_active_purpose_catalog():
    """Return active purposes and their registered historical aliases."""
    connection = get_connection()

    try:
        purposes = connection.execute("""
            SELECT id, name, expected_amount
            FROM payment_purposes
            WHERE status = 'Active'
            ORDER BY name
        """).fetchall()

        aliases = connection.execute("""
            SELECT purpose_id, alias_name
            FROM payment_purpose_aliases
            ORDER BY alias_name
        """).fetchall()

        aliases_by_id = {}

        for row in aliases:
            aliases_by_id.setdefault(row["purpose_id"], []).append(
                row["alias_name"]
            )

        return [
            {
                "id": row["id"],
                "name": row["name"],
                "expected_amount": float(row["expected_amount"] or 0),
                "aliases": list(dict.fromkeys(
                    [row["name"]] + aliases_by_id.get(row["id"], [])
                )),
            }
            for row in purposes
        ]

    finally:
        connection.close()


def resolve_payment_purpose(question):
    """
    Resolve a purpose using its canonical name or registered aliases.

    Returns:
        {"status": "matched", "purpose": {...}}
        {"status": "ambiguous", "purposes": [...]}
        {"status": "not_found", "purposes": [...]}
    """
    question_text = _normalise_text(question)
    catalog = get_active_purpose_catalog()

    matches = {}

    for purpose in catalog:
        for alias in purpose["aliases"]:
            alias_text = _normalise_text(alias)

            if not alias_text:
                continue

            pattern = (
                r"(?<![a-z0-9])"
                + re.escape(alias_text)
                + r"(?![a-z0-9])"
            )

            if re.search(pattern, question_text):
                matches[purpose["id"]] = purpose
                break

    matched = sorted(
        matches.values(),
        key=lambda item: len(item["name"]),
        reverse=True
    )

    if len(matched) == 1:
        return {
            "status": "matched",
            "purpose": matched[0],
        }

    if len(matched) > 1:
        return {
            "status": "ambiguous",
            "purposes": matched,
        }

    return {
        "status": "not_found",
        "purposes": catalog,
    }


def get_purpose_payment_report(purpose_id):
    """
    Return a complete payment report for one active purpose.

    Only Verified payments count towards a student's paid amount.
    Every Active student is included, including students with no payment.
    """
    connection = get_connection()

    try:
        purpose = connection.execute("""
            SELECT id, name, expected_amount
            FROM payment_purposes
            WHERE id = ?
              AND status = 'Active'
        """, (purpose_id,)).fetchone()

        if not purpose:
            return None

        expected_amount = float(purpose["expected_amount"] or 0)

        students = connection.execute("""
            SELECT
                s.id,
                s.matric_number,
                s.full_name,
                COALESCE(SUM(p.amount), 0) AS verified_paid
            FROM students s
            LEFT JOIN payments p
                ON p.student_id = s.id
               AND p.status = 'Verified'
               AND EXISTS (
                    SELECT 1
                    FROM payment_purpose_aliases a
                    WHERE a.purpose_id = ?
                      AND UPPER(TRIM(a.alias_name)) =
                          UPPER(TRIM(p.purpose))
               )
            WHERE s.status = 'Active'
            GROUP BY
                s.id,
                s.matric_number,
                s.full_name
            ORDER BY s.full_name
        """, (purpose_id,)).fetchall()

        paid_students = []
        verified_payers = []
        outstanding_students = []
        partial_students = []

        for student in students:
            paid = float(student["verified_paid"] or 0)

            record = {
                "matric_number": student["matric_number"],
                "full_name": student["full_name"],
                "verified_paid": paid,
                "expected_amount": expected_amount,
                "balance": max(expected_amount - paid, 0),
            }

            if paid > 0:
                verified_payers.append(record)

            if paid >= expected_amount and expected_amount > 0:
                paid_students.append(record)
            else:
                outstanding_students.append(record)

                if paid > 0:
                    partial_students.append(record)

        active_count = len(students)
        expected_total = expected_amount * active_count
        verified_collected = sum(
            item["verified_paid"] for item in students
        )

        return {
            "purpose_id": purpose["id"],
            "purpose": purpose["name"],
            "expected_amount_per_student": expected_amount,
            "active_students": active_count,
            "expected_total": expected_total,
            "verified_collected": verified_collected,
            "outstanding_total": sum(
                item["balance"] for item in outstanding_students
            ),
            "paid_count": len(paid_students),
            "outstanding_count": len(outstanding_students),
            "partial_count": len(partial_students),
            "paid_students": paid_students,
            "verified_payers": verified_payers,
            "outstanding_students": outstanding_students,
            "partial_students": partial_students,
        }

    finally:
        connection.close()


def find_active_student(search_text):
    """Find a student by exact matric number or an unambiguous name."""
    search = str(search_text or "").strip()

    if not search:
        return {"status": "not_found", "students": []}

    connection = get_connection()

    try:
        student = connection.execute("""
            SELECT id, matric_number, full_name
            FROM students
            WHERE status = 'Active'
              AND UPPER(TRIM(matric_number)) = UPPER(TRIM(?))
        """, (search,)).fetchone()

        if student:
            return {
                "status": "matched",
                "student": dict(student),
            }

        matches = connection.execute("""
            SELECT id, matric_number, full_name
            FROM students
            WHERE status = 'Active'
              AND LOWER(TRIM(full_name)) = LOWER(TRIM(?))
            ORDER BY full_name
        """, (search,)).fetchall()

        if len(matches) == 1:
            return {
                "status": "matched",
                "student": dict(matches[0]),
            }

        if len(matches) > 1:
            return {
                "status": "ambiguous",
                "students": [dict(row) for row in matches],
            }

        return {"status": "not_found", "students": []}

    finally:
        connection.close()


def get_student_payment_history(student_id):
    """Return a student's payment history without changing any records."""
    connection = get_connection()

    try:
        student = connection.execute("""
            SELECT id, matric_number, full_name
            FROM students
            WHERE id = ?
              AND status = 'Active'
        """, (student_id,)).fetchone()

        if not student:
            return None

        payments = connection.execute("""
            SELECT
                p.amount,
                p.purpose AS recorded_purpose,
                COALESCE(pp.name, p.purpose) AS recognised_purpose,
                p.payment_date,
                p.status,
                p.receipt_reference,
                p.verified_by,
                p.verified_at
            FROM payments p
            LEFT JOIN payment_purpose_aliases a
                ON UPPER(TRIM(a.alias_name)) =
                   UPPER(TRIM(p.purpose))
            LEFT JOIN payment_purposes pp
                ON pp.id = a.purpose_id
            WHERE p.student_id = ?
            ORDER BY p.payment_date DESC
        """, (student_id,)).fetchall()

        records = [
            {
                "amount": float(row["amount"] or 0),
                "recorded_purpose": row["recorded_purpose"],
                "recognised_purpose": row["recognised_purpose"],
                "payment_date": str(row["payment_date"]),
                "status": row["status"],
                "receipt_reference": row["receipt_reference"],
                "verified_by": row["verified_by"],
                "verified_at": (
                    str(row["verified_at"])
                    if row["verified_at"] else None
                ),
            }
            for row in payments
        ]

        return {
            "student": dict(student),
            "payments": records,
            "payment_count": len(records),
            "verified_count": sum(
                1 for item in records
                if item["status"] == "Verified"
            ),
        }

    finally:
        connection.close()


def get_payment_status_summary():
    """Return counts and amounts grouped by payment status."""
    connection = get_connection()

    try:
        rows = connection.execute("""
            SELECT
                status,
                COUNT(*) AS payment_count,
                COALESCE(SUM(amount), 0) AS total_amount
            FROM payments
            GROUP BY status
        """).fetchall()

        results = {
            status: {
                "payment_count": 0,
                "total_amount": 0.0,
            }
            for status in PAYMENT_STATUSES
        }

        for row in rows:
            status = row["status"]

            if status not in results:
                results[status] = {
                    "payment_count": 0,
                    "total_amount": 0.0,
                }

            results[status] = {
                "payment_count": int(row["payment_count"] or 0),
                "total_amount": float(row["total_amount"] or 0),
            }

        return results

    finally:
        connection.close()

def find_student_in_question(question):
    """Identify an Active student mentioned in a natural-language question."""
    query = _normalise_text(question)

    if not query:
        return {"status": "not_found", "students": []}

    connection = get_connection()

    try:
        students = connection.execute("""
            SELECT id, matric_number, full_name
            FROM students
            WHERE status = 'Active'
            ORDER BY full_name
        """).fetchall()

        matches = []

        for student in students:
            matric = _normalise_text(student["matric_number"])
            name = _normalise_text(student["full_name"])

            if (
                (matric and matric in query)
                or (name and name in query)
            ):
                matches.append(dict(student))

        if len(matches) == 1:
            return {
                "status": "matched",
                "student": matches[0],
            }

        if len(matches) > 1:
            return {
                "status": "ambiguous",
                "students": matches,
            }

        return {"status": "not_found", "students": []}

    finally:
        connection.close()
