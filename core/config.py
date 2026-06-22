# core/config.py
from pathlib import Path

SHEET_ID = "10azqdWsqw_E2K1lRRd7UTMFAba9zYDyRMY89DcSxt5A"  # Google Sheet ID
CRED_FILE = "svc.json"
CRED_FILE = str(Path(__file__).resolve().parent.parent / "svc.json")
SIMPLE_FORM_SHEET_ID = "1tcgLYk7Yqh4iKzD6IRp_RotmDsQKOltHZ4aF2oEj7t8" # Google Sheet ID for simple form responses
SIMPLE_FORM_TAB = "תגובות לטופס 1"
CLINIC_CAL_SHEET_ID = "1uKxfahirm9VZqFn-xApzgzCJAVOONOuF9vW7fUi5Q3M" # Google Sheet ID for hospital clinics calendar

# Optional separate Google Sheet for finalized roster imports.
# Recommended: create a dedicated spreadsheet named "Neuro Shift History",
# share it with the service-account email in svc.json, then paste its ID here.
# If left blank, history tabs are stored in SHEET_ID.
HISTORY_SHEET_ID = "1lhFGYHTUFhu-vG0WvoWxRC18mCfzf2o7RsKnIM1v5Vo"
HISTORY_TAB = "history"
HISTORY_SUMMARY_TAB = "history_summary"
