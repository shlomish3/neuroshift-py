from core.data import backend_tables
tbl = backend_tables()

# See exactly what we got from the sheet
print(sorted(tbl["required"]["סוג משמרת"].unique()))

# Or look at the index we use
from core.roster import SHIFT_TYPES
print([s for s in SHIFT_TYPES if "בוטו" in s])
