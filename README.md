# class-finance
for the class made with love 
Class Finance

A web-based finance management system designed to help manage student payments, payment purposes, receipts, and financial records for a class or student community.

Overview

Class Finance was developed as a practical student finance management application to simplify the process of recording, tracking, and verifying class payments.

The system provides a centralized interface for managing student financial records and uploaded payment receipts.

Features

- Student management
- Payment recording
- Payment-purpose management
- Individual student finance records
- Receipt uploads
- Receipt OCR using Tesseract
- Automatic extraction of payment details from receipts
- Payment verification based on detected receipt information
- Search and filtering of students
- Payment status tracking
- SQLite database storage
- Flask web application backend

Technology Stack

- Python
- Flask
- SQLite
- HTML
- CSS
- JavaScript
- Pillow
- pytesseract / Tesseract OCR

Project Structure

class-finance/
├── app.py
├── database.py
├── import_students.py
├── templates/
│   ├── add_payment.html
│   ├── dashboard.html
│   ├── finance.html
│   ├── payment_purposes.html
│   ├── student_finance.html
│   └── students.html
├── .gitignore
└── README.md

Payment Verification

Class Finance includes an OCR-assisted payment verification workflow.

When a receipt is uploaded, the system can attempt to extract information such as:

- Payment amount
- Payment date
- Transaction reference
- Sender
- Recipient
- Payment narration

The extracted information can then be compared with the submitted payment details to assist with verification and identify payments that may require manual review.

«OCR-assisted verification is intended as a supporting verification mechanism and does not replace human financial review.»

Development

This project is actively developed and maintained by:

Akinwale Akinfolarin Benjamin

Git history is maintained through Git and published through GitHub to document the project's development.

Status

🚧 Active Development

The application has been tested locally, including student management, payment recording, receipt upload, OCR extraction, and payment verification workflows.

License

This project is currently not licensed for unrestricted reuse or redistribution.
