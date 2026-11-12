"""Spreadsheet-safe text, shared by every CSV / XLSX producer (reports, exports, the import error
report). One implementation, so the rules cannot drift (decisions D-28h and D-28j).

Formula injection (R-30): a value is dangerous when its first character after any leading
whitespace or control characters is = + - @ (a leading tab or carriage return is dangerous too).
It is kept exactly as entered with an apostrophe in front. The only exemption is a strict phone
number. XLSX cannot hold most control characters, so ``xlsx_clean`` shows them as control pictures.
"""

import re

from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

FORMULA_STARTS = ("=", "+", "-", "@")
# Characters that may precede a formula character and are skipped when deciding whether a value
# is dangerous: every Unicode whitespace character (\s covers tab, CR, LF, NBSP, ideographic
# space), the C0/C1 control characters, DEL, and zero-width / format characters.
_IGNORABLE = "".join(
    [chr(c) for c in range(0x00, 0x20)]
    + [chr(0x7F)]
    + [chr(c) for c in range(0x80, 0xA0)]
    + [chr(c) for c in (0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0xFEFF)]
)
_LEADING = re.compile(rf"^(?:\s|[{re.escape(_IGNORABLE)}])*")
# A valid international phone number: '+' then digits, spaces and dots, with at least one digit.
# Nothing else is exempt (decision D-28h): '+1=2', '+cmd' and '+' alone are still escaped.
PHONE_RE = re.compile(r"^\+[\d .]*\d[\d .]*$")


def safe_text(value):
    """Escape a text value for CSV/XLSX (R-30). A value is dangerous when its first character
    after any leading whitespace or control characters is = + - @ (a leading tab or carriage
    return is dangerous too). The value is kept exactly as entered with an apostrophe in front.
    The strict phone-number pattern is the only exemption."""
    text = "" if value is None else str(value)
    if not text or PHONE_RE.fullmatch(text):
        return text
    rest = text[_LEADING.match(text).end() :]
    if text[0] in "\t\r" or (rest and rest[0] in FORMULA_STARTS):
        return "'" + text
    return text


def xlsx_clean(text):
    """XLSX cannot hold most control characters (they would crash the export). Each one is shown
    as its visible control picture (U+2400 + code, e.g. NUL -> the NUL symbol) so nothing is
    silently dropped. CSV and the database keep the original character."""
    return ILLEGAL_CHARACTERS_RE.sub(lambda m: chr(0x2400 + ord(m.group())), text)


def xlsx_text(value):
    """Escaped for formulas AND made storable in a workbook."""
    return xlsx_clean(safe_text(value))
