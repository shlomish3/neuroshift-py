"""
core/constants.py
-----------------
Pure configuration constants —  no network / gspread code here.
"""
from itertools import product

# ───────────────────────
# 1. Fair-distribution weekday desirability
#    (negative = attractive, positive = unattractive)
# ───────────────────────
WEEKDAY_BONUS = {
    "Mon":  0.00,
    "Tue":  0.00,
    "Wed": -0.20,   # premium night everyone likes
    "Thu":  0.40,
    "Fri":  1.00,
    "Sat":  1.40,
    "Sun":  0.30,
}

# small set used by assign.py when weekday bias should apply only to duties
BONUS_SHIFT_TYPES = {"ת.מיון", "כונן מיון", "ת.מיון 2"}

# ───────────────────────
# 2. Recency-penalty tuning
# ───────────────────────
NIGHT_DUTY_SHIFTS           = {"ת.מיון", "ת.מיון 2", "כונן מיון"}   # night duties
RECENCY_WINDOW_DAYS   = 15
RECENCY_PENALTY_MAX   = 7.5      

# “Nice” target weekdays reduce the pain of doing another duty soon
PENALTY_REDUCER = {
    "Wed": 0.50,   # if upcoming shift is Wed, halve the penalty
    # all other days default to 1.0
}

# ───────────────────────
# 3. Filling-priority buckets  (high → low)
# ───────────────────────
PRIORITY_BUCKETS = [
    "אטנדינג",
    "מרפאת תנועה", "מרפאת אפילפסיה גנדלמן", "מרפאת אפילפסיה הרש", "מרפאת CVA", "מרפאת קרוטיס",
    "מרפאת זיכרון", "מרפאת בוטוקס", "מרפאת נוירואימונולוגיה", "מרפאת עצב שריר",
    "מרפאת כאבי ראש", "מרפאת פוסט אשפוז", "מרפאת שבץ מוחי", "מרפאת נוירואונקולוגיה", "נוירולוגיה כללית",
    "EMG",
    "ת.מיון", "ת.מיון 2", "כונן מיון",
    "מיון",
    "מחלקה",
    "ייעוצים מובילים",
    "EEG",
]

# ───────────────────────
# 4. Same-day dual-shift rule
# ───────────────────────
_DAY_SHIFTS = {
    # clinics
    "EMG", "EEG",
    "מרפאת עצב שריר", "מרפאת תנועה", "מרפאת אפילפסיה גנדלמן", "מרפאת אפילפסיה הרש",
    "מרפאת CVA", "מרפאת קרוטיס", "מרפאת זיכרון",
    "מרפאת בוטוקס", "מרפאת נוירואימונולוגיה", "מרפאת כאבי ראש",
    "מרפאת פוסט אשפוז", "מרפאת שבץ מוחי", "מרפאת נוירואונקולוגיה", "נוירולוגיה כללית",
    # ward / ED / misc.
    "אטנדינג", "מחלקה", "מיון",
    "ייעוצים מובילים", "מחקר", "רוטציה",
}

def _both(pairs):
    """Return every pair (a, b) and its reverse (b, a)."""
    pairs_set = set(pairs)          # realise iterables exactly once
    return pairs_set | {(b, a) for (a, b) in pairs_set}

# day ↔ night (both directions)
_day_night_pairs = _both(product(_DAY_SHIFTS, NIGHT_DUTY_SHIFTS))

# 'בכיר מיון' with any day shift (both directions)
_bakir_pairs = _both({("בכיר מיון", s) for s in _DAY_SHIFTS}) ## Removed בכיר מיון entirely

# explicit extra day–day pairs
_extra_pairs = {
    ("EEG ילדים", "מרפאת אפילפסיה גנדלמן"), ("מרפאת אפילפסיה גנדלמן", "EEG ילדים"),
    ("EEG", "EEG ילדים"), ("EEG ילדים", "EEG"),
}

DUAL_OK = frozenset(_day_night_pairs | _bakir_pairs | _extra_pairs)

# ───────────────────────
# 5. Form source toggle
# ───────────────────────
# 1 = use the simpler Google Form ("requests" tab)
# 0 = use the legacy parsed_requests tab
USE_SIMPLE_FORM = 1
CURRENT_TARGET_MONTH: str | None = None  # e.g. "2025-11"

# ───────────────────────
# Email → Official Hebrew name
# ───────────────────────
EMAIL_TO_NAME = {
    "ardash.nat@gmail.com":        "ארדשירוב",
    "sofimdneuro@gmail.com":       "גלינסקיה",
    "nitai.shimon@gmail.com":      "שמעון",
    "shlomi.shmuel3@gmail.com":    "שמואל",
    "giladankori@gmail.com":       "קינן",
    "hossensaoub@gmail.com":       "סעוב",
    "shlomip@shamir.gov.il":       "פרץ",
    "itzhakk@shamir.gov.il":       "קימיאגר",
    "aviranpriante93@gmail.com":   "פריאנטה",
    "nirhersh@gmail.com":          "הרש",
    "coheno@shamir.gov.il":        "כהן",
    "oren.s.cohen@gmail.com":      "כהן",
    "eladhaser7@gmail.com":        "הסר",
    "aya_asly@hotmail.com":        "עסלי",
    "lakinsheli@gmail.com":        "לקן",
    "berg.assaf@gmail.com":        "ברג",
    "assaf.berg@mail.huji.ac.il":  "ברג",
    "gandelman@shamir.gov.il":     "גנדלמן",
    "nettastr@gmail.com":          "אגאג'ני",
    "liordekel3@gmail.com":        "דקל",
    "avigailbartal@hotmail.com":   "ברטל",
    "miniovitcha@shamir.gov.il":   "מיניוביץ'",
    "ahmad3x@hotmail.com":         "חדיג'ה",
    "albasantclinic@gmail.com":    "חדיג'ה",
    "hodayashir@gmail.com":        "שיר",
}
