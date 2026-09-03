import os
import sqlite3
from werkzeug.security import generate_password_hash

DATABASE = "class_finance.db"


def get_connection():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    connection = get_connection()

    connection.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            matric_number TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            purpose TEXT,
            payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            receipt_reference TEXT,
            receipt_file TEXT,
            status TEXT NOT NULL DEFAULT 'Pending',
            verification_note TEXT,
            verified_by TEXT,
            verified_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (student_id)
                REFERENCES students(id)
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS payment_purposes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            expected_amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'Active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            full_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    for i in range(1, 8):
        username = os.environ.get(f"ADMIN_BOOTSTRAP_USERNAME_{i}")
        full_name = os.environ.get(f"ADMIN_BOOTSTRAP_NAME_{i}")
        password = os.environ.get(f"ADMIN_BOOTSTRAP_PASSWORD_{i}")

        if username and full_name and password:
            existing = connection.execute(
                "SELECT id FROM admins WHERE username = ?",
                (username,)
            ).fetchone()

            if not existing:
                connection.execute(
                    """
                    INSERT INTO admins
                    (username, password_hash, full_name)
                    VALUES (?, ?, ?)
                    """,
                    (
                        username,
                        generate_password_hash(password),
                        full_name
                    )
                )

    connection.commit()
    connection.close()

if __name__ == "__main__":
    initialize_database()
    print("Database initialized successfully.")
