# core/config.py
from pathlib import Path

SHEET_ID = "10azqdWsqw_E2K1lRRd7UTMFAba9zYDyRMY89DcSxt5A"  # Google Sheet ID
CRED_FILE = "svc.json"
CRED_FILE = str(Path(__file__).resolve().parent.parent / "svc.json")
SIMPLE_FORM_SHEET_ID = "1tcgLYk7Yqh4iKzD6IRp_RotmDsQKOltHZ4aF2oEj7t8" # Google Sheet ID for simple form responses
SIMPLE_FORM_TAB = "תגובות לטופס 1"
CLINIC_CAL_SHEET_ID = "1uKxfahirm9VZqFn-xApzgzCJAVOONOuF9vW7fUi5Q3M" # Google Sheet ID for hospital clinics calendar
