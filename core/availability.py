"""
core/availability.py
====================

Tiny wrapper around the “responses” tab (“תגובות לטופס זמינות”).

• fetch_form()  →  up-to-date pandas DataFrame
• To force a cold reload, clear the backend_tables() cache first.
"""

import pandas as pd
from core.data import backend_tables


def fetch_form() -> pd.DataFrame:
    """
    Return the Google-Form responses as a DataFrame.

    The data comes from backend_tables()["requests"], which is already
    cached with a 5-minute TTL (or fully refreshed when the environment
    variable NEUROSHIFT_NOCACHE=1 is set).
    """
    return backend_tables()["requests"].copy()


# ──────────────────────────────────────────────────────────────
#  CLI smoke-test
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # force a hard refresh while testing
    backend_tables.cache_clear()

    df = fetch_form()
    print(f"Loaded {len(df):,} form responses")
    print(df.head())
