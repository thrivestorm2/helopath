"""US N-number <-> ICAO 24-bit address (Mode S hex), by the FAA's fixed allocation: N1 is a00001, N99999 is adf7c7.

N-numbers are N + a digit 1-9 + up to four more characters: digits, optionally ending in one or two letters
(never I or O), five characters at most. The FAA hands out the a00001-adf7c7 block in that order, so the hex is
computed, not looked up, and works for every US aircraft whether or not it is in the registry file we load."""

import re

LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O
FIRST = 0xA00001
SUFFIX = 1 + len(LETTERS) * (1 + len(LETTERS))   # nothing, one letter, or two letters: 601
BUCKET4 = 1 + len(LETTERS) + 10                  # after four digits: nothing, one letter, or one digit: 35
BUCKET3 = 10 * BUCKET4 + SUFFIX                  # 951
BUCKET2 = 10 * BUCKET3 + SUFFIX                  # 10111
BUCKET1 = 10 * BUCKET2 + SUFFIX                  # 101711
LAST = FIRST + 9 * BUCKET1 - 1                   # adf7c7

TAIL = re.compile(r"^N?([1-9][0-9]{0,4}|[1-9][0-9]{0,3}[A-HJ-NP-Z]|[1-9][0-9]{0,2}[A-HJ-NP-Z]{2})$")


def _suffix_offset(letters):
    if not letters:
        return 0
    offset = (len(LETTERS) + 1) * LETTERS.index(letters[0]) + 1
    if len(letters) == 2:
        offset += LETTERS.index(letters[1]) + 1
    return offset


def tail_to_hex(tail):
    """'N123AB' (or '123ab') -> six lower-case hex digits, or None if it is not a valid US N-number."""
    match = TAIL.match((tail or "").strip().upper().replace("-", ""))
    if not match:
        return None
    n = match.group(1)
    out = FIRST + (int(n[0]) - 1) * BUCKET1
    for pos, bucket in ((1, BUCKET2), (2, BUCKET3), (3, BUCKET4)):
        if len(n) == pos:
            return f"{out:06x}"
        if n[pos] in LETTERS:
            return f"{out + _suffix_offset(n[pos:]):06x}"
        out += SUFFIX + int(n[pos]) * bucket
    if len(n) == 4:
        return f"{out:06x}"
    last = n[4]
    out += 1 + (LETTERS.index(last) if last in LETTERS else len(LETTERS) + int(last))
    return f"{out:06x}"


def _suffix(r):
    if r == 0:
        return ""
    first, second = divmod(r - 1, len(LETTERS) + 1)
    return LETTERS[first] + (LETTERS[second - 1] if second else "")


def hex_to_tail(hexid):
    """Six hex digits -> 'N123AB', or None outside the US block a00001-adf7c7."""
    try:
        value = int((hexid or "").strip(), 16)
    except ValueError:
        return None
    if not FIRST <= value <= LAST or len(hexid.strip()) != 6:
        return None
    first, r = divmod(value - FIRST, BUCKET1)
    out = f"N{first + 1}"
    for bucket in (BUCKET2, BUCKET3, BUCKET4):
        if r < SUFFIX:
            return out + _suffix(r)
        digit, r = divmod(r - SUFFIX, bucket)
        out += str(digit)
    if r == 0:
        return out
    r -= 1
    return out + (LETTERS[r] if r < len(LETTERS) else str(r - len(LETTERS)))
