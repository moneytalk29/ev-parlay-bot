"""Core +EV math, shared by the Discord bot and the daily scanner.

EV% = (TrueProb * Payout - (1 - TrueProb)) * 100, where Payout = Decimal - 1.
Verdict: PLAY if EV% > 2, else SKIP.
"""
import re

EV_THRESHOLD = 2.0


def american_to_decimal(a):
    a = float(a)
    if abs(a) < 100:
        raise ValueError("American odds must be +100 or higher, or -100 or lower.")
    return a / 100 + 1 if a > 0 else 100 / abs(a) + 1


def decimal_to_american(d):
    d = float(d)
    if d <= 1:
        raise ValueError("Decimal odds must be above 1.")
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def fmt_american(a):
    a = int(round(a))
    return f"+{a}" if a > 0 else str(a)


def implied(decimal):
    return 1 / decimal


def ev_pct(true_prob, decimal):
    payout = decimal - 1
    return (true_prob * payout - (1 - true_prob)) * 100


def verdict(ev):
    return "PLAY" if ev > EV_THRESHOLD else "SKIP"


def devig(decimals):
    """No-vig true probabilities from one market's decimal prices (proportional method)."""
    imps = [1 / d for d in decimals]
    total = sum(imps)
    return [i / total for i in imps]


def evaluate_books(entries, true_prob):
    """entries: [(book, american)]. Returns rows sorted by EV desc; rows[0] is BEST BOOK."""
    rows = []
    for book, am in entries:
        d = american_to_decimal(am)
        e = ev_pct(true_prob, d)
        rows.append({"book": book, "american": int(am), "decimal": d,
                     "implied": implied(d), "ev": e, "verdict": verdict(e)})
    rows.sort(key=lambda r: r["ev"], reverse=True)
    return rows


def parlay(legs):
    """legs: [(american, true_prob)], 2 to 5 legs. Assumes independent legs."""
    if not 2 <= len(legs) <= 5:
        raise ValueError("A parlay needs 2 to 5 legs.")
    rows, dec, prob = [], 1.0, 1.0
    for am, p in legs:
        d = american_to_decimal(am)
        rows.append({"american": int(am), "decimal": d, "true_prob": p, "ev": ev_pct(p, d)})
        dec *= d
        prob *= p
    ev = ev_pct(prob, dec)
    return {"legs": rows, "decimal": dec, "american": decimal_to_american(dec),
            "win_prob": prob, "ev": ev, "verdict": verdict(ev)}


# ---------- text parsing for Discord commands ----------
def parse_prob(text):
    """'45', '45%' -> 0.45.  Values of 1 or less are read as a fraction ('0.45')."""
    s = str(text).strip().replace("%", "")
    x = float(s)
    p = x / 100 if x > 1 else x
    if not 0 < p < 1:
        raise ValueError("True probability must be between 0 and 100 (percent).")
    return p


def parse_book_odds(text):
    """'DraftKings +150, FanDuel +145' -> [('DraftKings', 150), ('FanDuel', 145)]."""
    out = []
    parts = [p.strip() for p in re.split(r"[,;\n]", text) if p.strip()]
    for i, part in enumerate(parts, start=1):
        part = part.replace(":", " ").replace("=", " ")
        m = re.match(r"^(?:(.*?)\s+)?([+-]?\d+)$", part.strip())
        if not m:
            raise ValueError(f"Couldn't read '{part}'. Use: DraftKings +150, FanDuel +145")
        book = (m.group(1) or f"Book {i}").strip()
        out.append((book, int(m.group(2))))
    if not out:
        raise ValueError("Add at least one book and odds, like: DraftKings +150")
    return out


def parse_legs(text):
    """'+150@45, -110@55' -> [(150, 0.45), (-110, 0.55)]."""
    out = []
    for part in [p.strip() for p in text.split(",") if p.strip()]:
        m = re.match(r"^([+-]?\d+)\s*[@/ ]\s*([\d.]+%?)$", part)
        if not m:
            raise ValueError(f"Couldn't read leg '{part}'. Use: +150@45, -110@55 (odds@true %)")
        out.append((int(m.group(1)), parse_prob(m.group(2))))
    if not 2 <= len(out) <= 5:
        raise ValueError("A parlay needs 2 to 5 legs.")
    return out
