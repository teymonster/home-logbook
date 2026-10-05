#!/usr/bin/env python3
"""Budget pipeline for the Home Logbook Sheet.

    python3 tools/budget.py pull                 # fetch log/bills/payments + transactions/receipts/budget → budget/cache/sheet.json
    python3 tools/budget.py backfill --months 12 # admin: harvest 12 months of alerts + receipt emails into the Sheet, month by month
    python3 tools/budget.py ingest               # read budget/statements/ (Chase PDF statements or CSV, USAA CSV) → budget/cache/csv.json
    python3 tools/budget.py match                # CSV ↔ alerts, bills, DoorDash/Uber/Amazon receipts → budget/cache/proposed.json
    python3 tools/budget.py push [--dry]         # write new/changed transactions (never tag/note) and receipt links to the Sheet
    python3 tools/budget.py report               # budget/report.md: monthly breakdown, Amazon + delivery panels, proposed budget
    python3 tools/budget.py run                  # pull → ingest → match → push → pull → report

Everything personal lives under budget/ (gitignored). This file holds no data and no secrets;
it reads .sync.json like tools/build.py and refuses to run if budget/ is tracked by git.
Transaction ids: e-<gmailId> for rows harvested from a Chase/Zelle alert, c-<hash> for a
statement line with no alert. Tags, notes and categories typed in the Sheet are never overwritten.
"""
import argparse
import collections
import csv
import datetime as dt
import hashlib
import itertools
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUDGET = os.path.join(ROOT, "budget")
STATEMENTS = os.path.join(BUDGET, "statements")
CACHE = os.path.join(BUDGET, "cache")
RULES = os.path.join(BUDGET, "rules.json")
REPORT = os.path.join(BUDGET, "report.md")
SYNC = os.path.join(ROOT, ".sync.json")

TAGS = ("necessary", "unnecessary", "frivolous", "skip")
KINDS = ("chase", "zelle", "amazon", "doordash", "uber")
NON_OWNED = ("date", "merchant", "amount", "account", "suggested", "detail", "billId", "source", "csv", "gmailId")
BATCH = 400


def die(msg):
    print("budget.py: " + msg, file=sys.stderr)
    sys.exit(1)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def d(s):
    return dt.date.fromisoformat(s)


def days(a, b):
    return (d(b) - d(a)).days


def money(x):
    return ("-$%s" if x < 0 else "$%s") % format(abs(x), ",.2f")


def guard_repo():
    try:
        tracked = subprocess.run(["git", "-C", ROOT, "ls-files", "budget"], capture_output=True, text=True).stdout.strip()
    except OSError:
        tracked = ""
    if tracked:
        die("budget/ is tracked by git; it must stay ignored (see .gitignore)")
    os.makedirs(STATEMENTS, exist_ok=True)
    os.makedirs(CACHE, exist_ok=True)


# ------------------------------------------------------------------------------------ sheet io

def cfg(admin=False):
    if not os.path.exists(SYNC):
        die(".sync.json missing")
    c = json.load(open(SYNC))
    if not c.get("url", "").startswith("https://script.google.com/") or len(c.get("token", "")) < 16:
        die(".sync.json needs url + token")
    if admin and len(c.get("admin", "")) < 16:
        die(".sync.json has no admin token; the email backfill needs it")
    return c


def http_json(url, data=None, timeout=330):
    """GET (or POST json) through curl: it follows the Apps Script redirect, honours the sandbox proxy and
    enforces --max-time, which a plain urllib socket timeout did not when the proxy swallowed a connection."""
    cmd = ["curl", "-sS", "-L", "--max-time", str(int(timeout)), "--retry", "0"]
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
    cmd.append(url)
    for attempt in range(1, 4):
        try:
            r = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout + 15)
            raw = r.stdout.decode("utf-8", "replace")
            if r.returncode == 0 and raw.lstrip().startswith("{"):
                return json.loads(raw)
            err = r.stderr.decode("utf-8", "replace").strip() or ("non-JSON response: " + raw[:80].replace("\n", " "))
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
            err = str(e)
        if attempt == 3:
            die("endpoint error: %s" % err)
        time.sleep(5 * attempt)


def sheet_get():
    c = cfg()
    j = http_json(c["url"] + "?" + urllib.parse.urlencode({"token": c["token"], "budget": "1"}))
    if not j.get("ok"):
        die("GET failed: %s" % j.get("error"))
    if j.get("version", 0) < 10:
        die("the Apps Script endpoint is v%s; v10 must be deployed first (paste tools/apps-script.gs, new version)" % j.get("version"))
    for k in ("transactions", "receipts", "budget", "bills", "payments"):
        j.setdefault(k, {})
    return j


def sheet_post(body):
    c = cfg()
    body = dict(body, token=c["token"], quiet=True)
    j = http_json(c["url"], data=json.dumps(body).encode("utf-8"))
    if not j.get("ok"):
        die("POST failed: %s" % j.get("error"))
    return j


def admin_get(timeout=330, **params):
    c = cfg(admin=True)
    params["admin"] = c["admin"]
    return http_json(c["url"] + "?" + urllib.parse.urlencode(params), timeout=timeout)


def load_cache(name, required=True):
    p = os.path.join(CACHE, name)
    if not os.path.exists(p):
        if required:
            die("%s missing; run the earlier step first" % os.path.relpath(p, ROOT))
        return None
    return json.load(open(p))


def save_cache(name, obj):
    p = os.path.join(CACHE, name)
    json.dump(obj, open(p, "w"), indent=1, sort_keys=True)
    return p


def live(coll):
    return {k: v for k, v in coll.items() if not k.startswith("__") and not v.get("del")}


# ------------------------------------------------------------------------------------ rules

DEFAULT_RULES = {
    "_doc": "Edit freely. merchants: first regex match wins (category + suggested tag). csvCategories map the bank's "
            "category to a budget category. billCategories map a bill's category to a default tag. amazonCategory / "
            "doordash map receipt words to a default tag. categories gives each budget category a default tag.",
    "merchants": [
        {"re": r"CHASE CREDIT CRD|AUTOPAY PAYMENT|Payment Thank You|USAA FUNDS TRANSFER|ONLINE TRANSFER|^CHECK$|INTERNET TRANSFER", "category": "transfer", "suggested": "skip"},
        {"re": r"PAYROLL|DIRECT DEP|TEXAS OAG|CHILD SUPPORT|INTEREST PAID|ATM REBATE|NAVAN|EXPENSIFY|DEPOSIT@MOBILE|MOBILE DEPOSIT", "category": "income", "suggested": ""},
        {"re": r"\bATM\b|CASH WITHDRAWAL|PAI ATM", "category": "cash", "suggested": ""},
        {"re": r"VENMO|CASH APP|ZELLE", "category": "p2p", "suggested": ""},
        {"re": r"DD \*DOORDASH|DOORDASH|GRUBHUB|UBER \*EATS|UBER EATS|INSTACART|HUNGRYROOT", "category": "delivery", "suggested": "unnecessary"},
        {"re": r"DASHPASS", "category": "subscription", "suggested": "unnecessary"},
        {"re": r"UBER(?! \*EATS)|LYFT|METRA|VENTRA|CTA |PACE BUS", "category": "rides", "suggested": ""},
        {"re": r"PRIME VIDEO|KINDLE SVCS|KINDLE UNLTD|AUDIBLE|AMAZON PRIME\*|AMAZON MUSIC", "category": "subscription", "suggested": ""},
        {"re": r"AMAZON|AMZN", "category": "amazon", "suggested": ""},
        {"re": r"WHOLEFDS|WHOLE FOODS|JEWEL|MARIANO|TRADER JOE|ALDI|COSTCO|SUNSET FOODS|WOODMANS|H-E-B|KROGER|HEINEN|FRESH MARKET", "category": "groceries", "suggested": "necessary"},
        {"re": r"TARGET|WALMART|WALGREENS|CVS/PHARM|CVS |HOMEDEPOT|HOME DEPOT|LOWES|MENARDS|ACE HDWE|ACE HARDWARE", "category": "household", "suggested": "necessary"},
        {"re": r"SHELL OIL|\bSHELL\b|BP#|\bMOBIL\b|EXXON|SPEEDWAY|CITGO|MARATHON|THORNTONS|CASEYS|PILOT_|BUC-EE|GAS/CARWASH|COSTCO GAS|WAWA|LOVE'S|KWIK TRIP|7-ELEVEN", "category": "gas", "suggested": "necessary"},
        {"re": r"MULLER AUTO|ADVANCE AUTO|AUTOZONE|JIFFY LUBE|FIRESTONE|DISCOUNT TIRE|CAR WASH|IL SOS|SECRETARY OF STA|TOLLWAY|I-PASS|EZ TAG|TOLL|PARKING|SPOTHERO", "category": "auto", "suggested": "necessary"},
        {"re": r"COMED|NORTH SHORE GAS|NICOR|CHARIOT ENERGY|CITY OF HIGHLAND PARK|HIGHLAND PARK WATER|MUNICIPAL ONLINE|LRS", "category": "utility", "suggested": "necessary"},
        {"re": r"ATT\*BILL|AT&T|VERIZON|T-MOBILE", "category": "phone", "suggested": "necessary"},
        {"re": r"COMCAST|XFINITY", "category": "internet", "suggested": "necessary"},
        {"re": r"MORTGAGE|ONITY", "category": "mortgage", "suggested": "necessary"},
        {"re": r"USAA P&C|USAA INSURANCE|INSURANCE", "category": "insurance", "suggested": "necessary"},
        {"re": r"GREEN ATTIC|PLUMBING|ELECTRICAL|HEATING|CAREFREE COMFORT|ROOFING|CHIMNEY|SEWER|HANDYMAN|QUICK KILL|LAWNSTARTER|LAWNCARE|TREE SERVICE|GUTTER", "category": "house", "suggested": "necessary"},
        {"re": r"LOVESAC|WAYFAIR|IKEA|CRATE|POTTERY BARN|ASHLEY FURN|RESTORATION HARD", "category": "furniture", "suggested": ""},
        {"re": r"DENTAL|THERAPY|WELLNESS|CLINIC|PHYSICIAN|HOSPITAL|PHARMACY|OPTICAL|ZENNI|URGENT CARE|LABCORP|QUEST DIAG|MEDICAL|ORTHO|DERMAT|COUNSELING", "category": "health", "suggested": "necessary"},
        {"re": r"NAILS|SPA |SPA$|SALON|BARBER|MASSAGE|HAIR|OMNILUX|SEPHORA|ULTA", "category": "personal", "suggested": ""},
        {"re": r"NETFLIX|HULU|SPOTIFY|PARAMOUNT|PEACOCK|DISNEY|APPLE\.COM/BILL|CURIOSITY|MIDJOURNEY|OPENAI|CHATGPT|GODADDY|OURPACT|PLUME|ROOST|EHARMONY|BARK TECHNOLOGIES|OUR FAMILY WIZARD|WALMART\+|ADOBE|INTUIT|PLAUD|MIRANTIS|PATREON|SUBSTACK|YOUTUBE|GOOGLE \*|MICROSOFT", "category": "subscription", "suggested": ""},
        {"re": r"FITNESS DEPOT", "category": "membership", "suggested": "unnecessary"},
        {"re": r"LIFE TIME|LIFETIME|LTFITNESS|PLANET FIT|YMCA", "category": "membership", "suggested": ""},
        {"re": r"NSSD112|NORTH SHORE SCHOOL|SCHOOL|CAMP |ART CENTER|IMPERISOFT|ACTIVE NETWORK|QUINLAN AND FABISH|FIVE BELOW|LEARNING|TUTOR|SCOUTS", "category": "kids", "suggested": "necessary"},
        {"re": r"ALASKA AIR|SOUTHWES|UNITED|DELTA|AMERICAN AIR|PRICELN|HOTEL|HTL|INN |INN$|LODGE|RESORT|MARRIOTT|HILTON|HYATT|AIRBNB|VRBO|CAMPING|CAMPGROUND|AMTRAK|HEADOUT|EXPEDIA|TRIPADVISOR|HERTZ|ENTERPRISE RENT|NIAGARA", "category": "travel", "suggested": ""},
        {"re": r"STUBHUB|TICKETMASTER|AMC |CINEMA|THEATRE|THEATER|MUSEUM|ZOO|BOWL|ARCADE|STEAM GAMES|NINTENDO|PLAYSTATION|XBOX", "category": "entertainment", "suggested": "unnecessary"},
        {"re": r"TEMU|SHEIN|ETSY|EBAY|GOODWILL|MARSHALLS|TJ MAXX|ROSS |KOHLS|OLD NAVY|GAP |NORDSTROM|MACYS|DOCKERS|HOBBY-LOBBY|HOBBY LOBBY|MICHAELS|BEST BUY|APPLE STORE|4TE|RVLOCK|SHAMAN MOD", "category": "shopping", "suggested": "unnecessary"},
        {"re": r"STARBUCKS|DUNKIN|PEET|COFFEE|CARIBOU", "category": "coffee", "suggested": "unnecessary"},
        {"re": r"TST\*|TST\* |TOCK |MCDONALD|PANERA|CHIPOTLE|POTBELLY|PORTILLO|PANDA EXPRESS|WENDY|TACO BELL|CULVER|JIMMY JOHN|SUBWAY|RESTAURA|GRILL|PIZZA|SUSHI|CAFE|BISTRO|TAVERN|BAR & |BURGER|OLIVE GARDEN|CHICK-FIL|DAIRY QUEEN|GIORDANO|LOU MALNATI|NOODLES|QDOBA|STEAK|KITCHEN|DINER|BAKERY|DONUT|ICE CREAM|SQ \*", "category": "dining", "suggested": "unnecessary"},
        {"re": r"CURALEAF|ZEN LEAF|WINDY CITY|AROMA HILL|DISPENSARY|SUNNYSIDE|RISE |CANNAVERSE|VERILIFE|NUERA", "category": "dispensary", "suggested": ""},
        {"re": r"PIRATE SHIP|USPS|UPS STORE|FEDEX|THE UPS", "category": "shipping", "suggested": ""},
        {"re": r"WORLD WILDLIFE|DONATION|ACLU|RED CROSS|GOFUNDME|CHARITY", "category": "giving", "suggested": "unnecessary"},
        {"re": r"NOTARIES|LAND TRUST|LEGAL|ATTORNEY|LAW OFFICE|H&R BLOCK|TURBOTAX|ACCOUNTANT", "category": "services", "suggested": ""},
        {"re": r"PAYPAL \*|PP\*", "category": "shopping", "suggested": ""},
    ],
    "csvCategories": {
        "groceries": "groceries", "gas": "gas", "food & drink": "dining", "fast food": "dining", "restaurants": "dining",
        "coffee shops": "coffee", "shopping": "shopping", "home": "household", "bills & utilities": "utility", "utilities": "utility",
        "mortgage & rent": "mortgage", "travel": "travel", "hotel": "travel", "entertainment": "entertainment",
        "health & wellness": "health", "health & fitness": "membership", "personal care": "personal", "personal": "personal",
        "education": "kids", "gifts & donations": "giving", "automotive": "auto", "professional services": "services",
        "fees & adjustments": "fees", "fees & charges": "fees", "atm fee": "fees", "cash": "cash", "check": "cash",
        "transfer": "transfer", "credit card payment": "transfer", "paycheck": "income", "income": "income", "child support": "income",
        "interest income": "income", "financial": "income", "shipping": "shipping", "uncategorized": "", "category pending": ""
    },
    "categories": {
        "groceries": "necessary", "gas": "necessary", "utility": "necessary", "mortgage": "necessary", "insurance": "necessary",
        "household": "necessary", "kids": "necessary", "phone": "necessary", "internet": "necessary", "trash": "necessary",
        "pest": "necessary", "lawn": "necessary", "tax": "necessary", "health": "necessary", "auto": "necessary", "house": "necessary",
        "furniture": "", 
        "dining": "unnecessary", "delivery": "unnecessary", "coffee": "unnecessary", "shopping": "unnecessary", "entertainment": "unnecessary",
        "transfer": "skip", "income": "", "cash": "", "p2p": "", "amazon": "", "subscription": "", "membership": "", "streaming": "",
        "rides": "", "travel": "", "personal": "", "dispensary": "", "shipping": "", "giving": "", "services": "", "fees": "", "card": "skip", "other": ""
    },
    "billCategories": {
        "utility": "necessary", "mortgage": "necessary", "insurance": "necessary", "phone": "necessary", "internet": "necessary",
        "trash": "necessary", "household": "necessary", "kids": "necessary", "tax": "necessary", "pest": "necessary", "lawn": "necessary",
        "card": "skip", "streaming": "", "subscription": "", "membership": "", "classes": "", "other": ""
    },
    "amazonCategory": {
        "Household Supplies": "necessary", "Plumbing": "necessary", "Home Safety": "necessary", "Home Improvement": "necessary",
        "Hand Tools": "necessary", "Appliances": "necessary", "Vacuum Accessories": "necessary", "Health": "necessary",
        "Personal Care": "necessary", "Grocery": "necessary", "Pet Supplies": "necessary", "School Supplies": "necessary",
        "Clothing": "unnecessary", "Supplements": "unnecessary", "Arts & Crafts": "unnecessary", "Toys": "unnecessary",
        "Books": "unnecessary", "Digital": "unnecessary", "Electronics": "", "Kitchen": "", "Beauty": "", "Sports": ""
    },
    "doordash": {"Target": "necessary", "Jewel-Osco": "necessary", "Walgreens": "necessary", "CVS": "necessary", "Aldi": "necessary",
                 "Mariano's": "necessary", "Costco": "necessary", "Whole Foods": "necessary"},
    "amazonItems": {"necessary": ["filter", "vacuum", "plumb", "faucet", "bulb", "battery", "detergent", "trash bag", "toilet", "paper towel",
                                  "toothpaste", "shampoo", "vitamin", "medicine", "first aid", "smoke", "carbon monoxide", "furnace", "humidifier",
                                  "dehumidifier", "thermostat", "school", "backpack", "notebook", "pencil", "printer", "ink", "dog food", "cat food", "litter"],
                    "unnecessary": ["romance", "novel", "book", "hoodie", "shirt", "shoe", "boot", "sneaker", "converse", "earring", "jewelry",
                                    "crochet", "craft", "toy", "game", "fuggler", "decor", "candle", "pre-workout", "supplement", "telescope", "costume",
                                    "lego", "plush", "poster"]}
}


def rules():
    if not os.path.exists(RULES):
        json.dump(DEFAULT_RULES, open(RULES, "w"), indent=1)
        print("wrote default rules to %s — edit as needed" % os.path.relpath(RULES, ROOT))
    r = json.load(open(RULES))
    for k, v in DEFAULT_RULES.items():
        r.setdefault(k, v)
    r["_compiled"] = [(re.compile(m["re"], re.I), m.get("category", ""), m.get("suggested", "")) for m in r["merchants"]]
    return r


def rule_for(merchant, r):
    for rx, cat, sug in r["_compiled"]:
        if rx.search(merchant or ""):
            return cat, sug
    return "", ""


def tag_ok(x):
    return x if x in TAGS else ""


# ------------------------------------------------------------------------------------ ingest

CHASE_HEADER = ["Transaction Date", "Post Date", "Description", "Category", "Type", "Amount", "Memo"]
USAA_HEADER = ["Date", "Description", "Original Description", "Category", "Amount", "Status"]


def us_date(x):
    x = x.strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", x)
    if m:
        return "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2)))
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", x)
    if m:
        return m.group(0)
    raise ValueError("bad date %r" % x)


def clean_desc(x):
    x = re.sub(r"\s+", " ", x or "").strip()
    return x[:80]


def parse_statement(path):
    """Return (account, rows). Rows: date, postDate, desc, csvCat, amount (+ = spend), type, status."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return None, []
    header = [h.strip() for h in rows[0]]
    out = []
    if header[:6] == CHASE_HEADER[:6]:
        for r in rows[1:]:
            r = (r + [""] * 7)[:7]
            typ = r[4].strip()
            if typ.lower() == "payment":
                continue  # card payments are counted once, on the bank side, as a transfer
            amt = -float(r[5].replace(",", "") or 0)
            out.append({"date": us_date(r[0]), "postDate": us_date(r[1]) if r[1].strip() else us_date(r[0]), "desc": clean_desc(r[2]),
                        "csvCat": r[3].strip(), "amount": round(amt, 2), "type": typ, "status": "Posted", "memo": r[6].strip()})
        return "chase", out
    if header == USAA_HEADER:
        for r in rows[1:]:
            r = (r + [""] * 6)[:6]
            amt = -float(r[4].replace(",", "").replace("$", "") or 0)
            status = "Pending" if r[5].strip().lower() in ("p", "pending") else "Posted"
            out.append({"date": us_date(r[0]), "postDate": us_date(r[0]), "desc": clean_desc(r[2] or r[1]), "csvCat": r[3].strip(),
                        "amount": round(amt, 2), "type": "Debit" if amt > 0 else "Credit", "status": status, "memo": ""})
        return "usaa", out
    die("%s: unrecognised header %s (expected Chase %s or USAA %s)" % (os.path.basename(path), header, CHASE_HEADER, USAA_HEADER))


# Chase monthly statement PDFs (pdftotext -layout, wide crop). Transactions sit under ACCOUNT ACTIVITY in
# "MM/DD  description  amount" lines; Amazon charges carry an "Order Number …" continuation line.
# The rewards section repeats the purchases, so parsing stops at the interest/rewards tables.
ORDER_NO_RE = re.compile(r"^\s+Order Number\s+([A-Z0-9]{3}-\d{7}-\d{7})\s*$")
TX_LINE_RE = re.compile(r"^\s*(\d{2})/(\d{2})\s+(.+?)\s{2,}(-?)\$?([\d,]*\.\d{2})\s*$")   # amounts under $1 print as ".64"
STOP_RE = re.compile(r"^\s*(INTEREST CHARGES|Your Annual Percentage Rate|PURCHASES AND REDEMPTIONS|\d{4} Totals Year-to-Date|Year-to-date totals)", re.I)
SECTION_RE = re.compile(r"^\s*(PAYMENTS AND OTHER CREDITS|PURCHASE|FEES CHARGED|INTEREST CHARGED)\s*$")
PAYMENT_DESC_RE = re.compile(r"Payment Thank You|AUTOMATIC PAYMENT|AUTOPAY|ONLINE PAYMENT", re.I)


def pdf_text(path):
    try:
        # A wide crop box: with the page's own width, -layout clips the right edge and amounts lose digits.
        return subprocess.run(["pdftotext", "-layout", "-W", "2000", "-H", "3000", path, "-"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as e:
        die("pdftotext failed on %s: %s (install poppler-utils)" % (os.path.basename(path), e))


def parse_chase_text(text):
    """Chase statement text → (rows, summary). summary has purchases/credits totals from page 1 for a self-check."""
    m = re.search(r"Opening/Closing Date\s+(\d{2})/(\d{2})/(\d{2})\s*-\s*(\d{2})/(\d{2})/(\d{2})", text)
    if not m:
        raise ValueError("no Opening/Closing Date line")
    open_y, close_m, close_y = 2000 + int(m.group(3)), int(m.group(4)), 2000 + int(m.group(6))

    def full_date(mm, dd):
        y = close_y if mm <= close_m or open_y == close_y else open_y
        if open_y != close_y and mm > close_m:
            y = open_y
        return "%04d-%02d-%02d" % (y, mm, dd)

    summary = {}
    for label, key in (("Purchases", "purchases"), ("Payment, Credits", "credits"), ("Fees Charged", "fees"), ("Interest Charged", "interest")):
        sm = re.search(r"^\s*" + re.escape(label) + r"\s+([-+]?)\$([\d,]+\.\d{2})\s*$", text, re.M)
        if sm:
            summary[key] = float(sm.group(2).replace(",", "")) * (-1 if sm.group(1) == "-" else 1)
    rows, section, active = [], None, False
    for line in text.splitlines():
        if re.match(r"^\s*ACCOUNT ACTIVITY", line):
            active = True
            continue
        if not active:
            continue
        if STOP_RE.match(line):
            break
        sm = SECTION_RE.match(line)
        if sm:
            section = sm.group(1)
            continue
        om = ORDER_NO_RE.match(line)
        if om and rows:
            rows[-1]["orderId"] = om.group(1)
            continue
        tm = TX_LINE_RE.match(line)
        if not tm or section is None:
            continue
        amt = float(tm.group(5).replace(",", "")) * (-1 if tm.group(4) == "-" else 1)
        desc = clean_desc(tm.group(3))
        if section == "PAYMENTS AND OTHER CREDITS" and PAYMENT_DESC_RE.search(desc):
            continue  # card payments are counted once, on the bank side, as a transfer
        typ = {"PAYMENTS AND OTHER CREDITS": "Return", "PURCHASE": "Sale", "FEES CHARGED": "Fee", "INTEREST CHARGED": "Interest"}[section]
        date = full_date(int(tm.group(1)), int(tm.group(2)))
        rows.append({"date": date, "postDate": date, "desc": desc, "csvCat": "", "amount": round(amt, 2), "type": typ, "status": "Posted", "memo": "", "orderId": ""})
    return rows, summary


def parse_chase_pdf(path):
    rows, summary = parse_chase_text(pdf_text(path))
    got_p = round(sum(r["amount"] for r in rows if r["type"] == "Sale"), 2)
    want_p = summary.get("purchases")
    note = ""
    if want_p is not None and abs(got_p - want_p) > 0.01:
        note = "  CHECK: parsed purchases %s vs statement %s" % (money(got_p), money(want_p))
    return rows, note


def csv_id(account, row, n):
    key = "%s|%s|%.2f|%s|%d" % (account, row["date"], row["amount"], row["desc"], n)
    return "c-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def cmd_ingest(_args):
    files = sorted(f for f in os.listdir(STATEMENTS) if f.lower().endswith((".csv", ".pdf")))
    if not files:
        die("no CSV or PDF statements in %s" % os.path.relpath(STATEMENTS, ROOT))
    per_file = []
    for f in files:
        note = ""
        if f.lower().endswith(".pdf"):
            account, (rows, note) = "chase", parse_chase_pdf(os.path.join(STATEMENTS, f))
        else:
            account, rows = parse_statement(os.path.join(STATEMENTS, f))
        if account:
            per_file.append((f, account, rows))
            print("%-45s %-5s %4d rows  %s → %s%s" % (f, account, len(rows), min(r["date"] for r in rows) if rows else "-", max(r["date"] for r in rows) if rows else "-", note))
    # Overlapping exports repeat the same lines: keep, per identical line, the largest count any one file has.
    merged, coverage = {}, {}
    for f, account, rows in per_file:
        counts = collections.Counter()
        for r in rows:
            key = (account, r["date"], r["amount"], r["desc"])
            counts[key] += 1
            n = counts[key]
            rid = csv_id(account, r, n)
            merged[rid] = dict(r, account=account, id=rid, file=f)
        if rows:
            lo, hi = min(r["date"] for r in rows), max(r["date"] for r in rows)
            c = coverage.setdefault(account, [lo, hi])
            c[0], c[1] = min(c[0], lo), max(c[1], hi)
    out = {"rows": merged, "coverage": coverage, "ingested": now_iso()}
    p = save_cache("csv.json", out)
    print("%d statement lines → %s; coverage %s" % (len(merged), os.path.relpath(p, ROOT), coverage))
    return out


# ------------------------------------------------------------------------------------ backfill

def month_windows(months):
    today = dt.date.today()
    first = (today.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    for _ in range(months - 1):
        first = (first - dt.timedelta(days=1)).replace(day=1)
    wins, cur = [], first
    while cur <= today:
        nxt = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        wins.append((cur.isoformat(), min(nxt, today + dt.timedelta(days=1)).isoformat()))
        cur = nxt
    return wins


def scan_window(kind, after, before, dry):
    """One admin budgetscan; split the window when Gmail truncates it."""
    for attempt in range(1, 4):
        j = admin_get(op="budgetscan", kind=kind, after=after, before=before, max=300, dry="1" if dry else "0")
        if j.get("ok"):
            break
        # "unknown op" right after a redeploy means a stale version answered; transient errors also retry.
        print("  %s %s..%s ERROR %s%s" % (kind, after, before, j.get("error"), " (retrying)" if attempt < 3 else ""))
        time.sleep(5 * attempt)
    if not j.get("ok"):
        return {"added": 0, "dup": 0, "warnings": ["%s %s..%s failed: %s" % (kind, after, before, j.get("error"))]}
    added, dup = len(j.get("added", [])), j.get("skipped", {}).get("dup", 0)
    print("  %-8s %s..%s  checked %3d  added %3d  dup %3d%s" % (kind, after, before, j.get("checked", 0), added, dup, "  TRUNCATED" if j.get("truncated") else ""))
    res = {"added": added, "dup": dup, "warnings": j.get("warnings", [])}
    if j.get("truncated") and days(after, before) > 1:
        mid = (d(after) + dt.timedelta(days=days(after, before) // 2)).isoformat()
        for a, b in ((after, mid), (mid, before)):
            r = scan_window(kind, a, b, dry)
            res["added"] += r["added"]; res["dup"] += r["dup"]; res["warnings"] += r["warnings"]
    return res


def cmd_backfill(args):
    kinds = KINDS if args.kind == "all" else tuple(k for k in args.kind.split(",") if k in KINDS)
    wins = month_windows(args.months)
    print("backfill %s over %d windows (%s → %s)%s" % (",".join(kinds), len(wins), wins[0][0], wins[-1][1], " DRY" if args.dry else ""))
    total = collections.Counter()
    warnings = []
    for after, before in wins:
        for kind in kinds:
            r = scan_window(kind, after, before, args.dry)
            total["added"] += r["added"]; total["dup"] += r["dup"]
            warnings += r["warnings"]
            time.sleep(1)
    print("done: added %d, already present %d, warnings %d" % (total["added"], total["dup"], len(warnings)))
    for w in warnings[:40]:
        print("  warn:", w)


# ------------------------------------------------------------------------------------ receipt repair

AMZ_ORDER_PY = re.compile(r"Order\s*#\s*:?\s*([A-Z0-9]{3}-\d{7}-\d{7})([\s\S]{0,2500}?)(?:Grand Total|Order Total|Total)\s*:?\s*\$?\s*([\d,]+(?:\.\d{1,2})?)\s*(?:USD)?", re.I)
AMZ_ITEM_PY = re.compile(r"\*\s*([^*\n]{2,120}?)\s+Quantity:\s*(\d+)")
AMZ_REFUND_PY = re.compile(r"\$([\d,]+\.\d{2}) (?:will be|has been|was) (?:credited|refunded|issued)|(?:refund(?: total)?|total refund)\*?\s*:?\s*\$([\d,]+\.\d{2})", re.I)


def parse_amazon_py(subject, body):
    """Python twin of the script's parseAmazon, for repairing receipts the deployed version mis-read."""
    subj = subject.strip()
    if re.search(r"dropoff|drop[- ]off|return (request|received|summary|confirmation)|printing information|pickup|pick-up|label|refund ineligible", subj, re.I) and not re.search(r"refund issued", subj, re.I):
        return []
    if re.search(r"refund", subj, re.I):
        rm = AMZ_REFUND_PY.search(body)
        om = re.search(r"orderId=([A-Z0-9]{3}-\d{7}-\d{7})", body)
        item = (re.search(r"refund issued for (.+?)\.*$", subj, re.I) or [None, ""])[1]
        total = -float((rm.group(1) or rm.group(2)).replace(",", "")) if rm else None
        return [{"kind": "amazon-refund", "merchant": "Amazon refund", "total": total, "orderId": om.group(1) if om else "", "categories": "", "items": item[:200]}]
    cats = (re.search(r"^Ordered \d+ items?:\s*(.+)$", subj, re.I) or [None, ""])[1]
    cats = re.sub(r",?\s*and more$", "", cats, flags=re.I).strip()
    digital = (re.search(r"order of (.+?)\.{0,3}$", subj, re.I) or [None, ""])[1]
    named = re.search(r'^Ordered:\s*\d*\s*"(.+?)"(?:\s*and (\d+) more items?)?', subj, re.I)
    out = []
    for m in AMZ_ORDER_PY.finditer(body):
        items = [(q + "x " if q != "1" else "") + name.strip() for name, q in AMZ_ITEM_PY.findall(m.group(2))][:12]
        text = digital or "; ".join(items) or ((named.group(1) + (" and %s more" % named.group(2) if named.group(2) else "")) if named else "")
        out.append({"kind": "amazon", "merchant": "Amazon digital" if digital else "Amazon", "total": float(m.group(3).replace(",", "")),
                    "orderId": m.group(1), "categories": "Digital" if digital else cats, "items": text[:300]})
    if not out:
        out.append({"kind": "amazon", "merchant": "Amazon digital" if digital else "Amazon", "total": None, "orderId": "",
                    "categories": "Digital" if digital else cats, "items": digital[:200], "unparsed": True})
    return out


def cmd_rescan_receipts(args):
    """Re-read Amazon receipts that have no order id or no total (older deployed parser) through the peek op."""
    guard_repo()
    sheet = load_cache("sheet.json")
    rc = live(sheet["receipts"])
    todo = {k: v for k, v in rc.items() if v["kind"] in ("amazon", "amazon-refund")
            and (not v.get("orderId") or v.get("total") is None or (v["kind"] == "amazon" and not v.get("categories") and not v.get("items")))}
    print("%d Amazon receipts to re-read%s" % (len(todo), " (DRY)" if args.dry else ""))
    stamp = now_iso()
    new_rows, tombstones, still = {}, {}, []
    for i, (rid, v) in enumerate(sorted(todo.items(), key=lambda kv: kv[1]["date"])):
        gid = v.get("gmailId") or (rid[2:] if rid.startswith("r-") else "")
        if not gid:
            still.append((rid, "no gmailId"))
            continue
        try:
            p = admin_get(op="peek", id=gid, timeout=45)
        except SystemExit:
            still.append((rid, "endpoint error"))
            continue
        if not p.get("ok"):
            still.append((rid, p.get("error")))
            continue
        recs = parse_amazon_py(p.get("subject", ""), p.get("body", ""))
        if not recs:
            tombstones[rid] = {"del": True, "u": stamp}      # return logistics: not money, and stays "seen"
            continue
        if any(r.get("unparsed") or r["total"] is None for r in recs):
            still.append((rid, "unparsed: " + p.get("subject", "")[:60]))
            continue
        for r in recs:
            nid = "a-" + r["orderId"] if r["kind"] == "amazon" and r["orderId"] else rid
            new_rows[nid] = {"kind": r["kind"], "date": v["date"], "merchant": r["merchant"], "total": r["total"], "orderId": r["orderId"],
                             "categories": r["categories"], "items": r["items"], "last4": "", "gmailId": gid, "txId": v.get("txId", "") if nid == rid else "", "u": stamp}
        if rid not in new_rows:
            tombstones[rid] = {"del": True, "u": stamp}
        if args.dry and i < 8:
            print("  %s → %s" % (rid, [(r["kind"], r["orderId"], r["total"], r["items"][:40]) for r in recs]))
        if (i + 1) % 25 == 0:
            print("  %d read (%d repaired, %d retired, %d unreadable)" % (i + 1, len(new_rows), len(tombstones), len(still)))
            if not args.dry:
                flush_receipts(new_rows, tombstones)
    print("repaired %d rows, %d placeholders retired, %d still unreadable" % (len(new_rows), len(tombstones), len(still)))
    for rid, why in still[:30]:
        print("  still:", rid, why)
    if args.dry:
        return
    flush_receipts(new_rows, tombstones)
    cmd_pull(args)


def flush_receipts(new_rows, tombstones):
    """Push what the repair has so far, then forget it, so a restart never loses or repeats work."""
    body = dict(new_rows)
    body.update(tombstones)
    ids = list(body)
    for i in range(0, len(ids), BATCH):
        sheet_post({"receipts": {k: body[k] for k in ids[i:i + BATCH]}})
    if ids:
        print("  pushed %d receipt rows" % len(ids))
    new_rows.clear()
    tombstones.clear()


# ------------------------------------------------------------------------------------ match

AMAZON_RE = re.compile(r"AMAZON|AMZN", re.I)
DD_RE = re.compile(r"DOORDASH|DD \*", re.I)
UBER_RE = re.compile(r"UBER", re.I)


def tokens(x):
    return {t for t in re.split(r"[^A-Z0-9]+", (x or "").upper()) if len(t) >= 4 and not t.isdigit()}


def overlap(a, b):
    return len(tokens(a) & tokens(b))


class Matcher:
    def __init__(self, sheet, csvdata, r):
        self.r = r
        self.bills = live(sheet["bills"])
        self.payments = live(sheet["payments"])
        self.budget = live(sheet["budget"])
        self.sheet_tx = live(sheet["transactions"])
        self.receipts = live(sheet["receipts"])
        self.tx = {k: dict(v) for k, v in self.sheet_tx.items()}
        self.csv = csvdata["rows"] if csvdata else {}
        self.coverage = csvdata["coverage"] if csvdata else {}
        self.receipt_links = {}
        self.notes = collections.defaultdict(list)   # diagnostics for the report

    # -- step 1: statement lines -------------------------------------------------------------
    def apply_csv(self):
        alerts = [k for k, v in self.tx.items() if v.get("source") in ("alert", "zelle")]
        used = set()
        for rid, row in sorted(self.csv.items(), key=lambda kv: kv[1]["date"]):
            if rid in self.tx:
                t = self.tx[rid]
                t.update(csv=True, date=row["date"], amount=row["amount"], _csvCat=row["csvCat"], _orderId=row.get("orderId", ""))
                continue
            best = None
            for k in alerts:
                t = self.tx[k]
                if k in used or t.get("account") != row["account"] or abs(t["amount"] - row["amount"]) > 0.005:
                    continue
                gap = abs(days(t["date"], row["date"]))
                if gap > 3:
                    continue
                score = (gap, -overlap(t["merchant"], row["desc"]))
                if best is None or score < best[0]:
                    best = (score, k)
            if best:
                k = best[1]
                used.add(k)
                self.tx[k]["csv"] = True
                self.tx[k]["_csvCat"] = row["csvCat"]
                if row.get("orderId"):
                    self.tx[k]["_orderId"] = row["orderId"]
                continue
            self.tx[rid] = {"date": row["date"], "merchant": row["desc"], "amount": row["amount"], "account": row["account"], "category": "",
                            "suggested": "", "tag": "", "note": "", "detail": "", "billId": "", "source": "csv", "csv": True, "gmailId": "",
                            "_csvCat": row["csvCat"], "_status": row["status"], "_orderId": row.get("orderId", "")}
        # Second pass: alerts whose posted amount differs (DoorDash adjustments, tips added later).
        fresh = [k for k in self.tx if k.startswith("c-") and self.tx[k].get("source") == "csv" and k in self.csv]
        for k in alerts:
            if k in used:
                continue
            t = self.tx[k]
            best = None
            for c in fresh:
                row = self.tx.get(c)
                if row is None or row.get("account") != t.get("account") or c in used:
                    continue
                if abs(days(t["date"], row["date"])) > 4 or overlap(t["merchant"], row["merchant"]) == 0:
                    continue
                diff = abs(row["amount"] - t["amount"])
                if diff > max(3.0, 0.15 * abs(t["amount"])):
                    continue
                if best is None or diff < best[0]:
                    best = (diff, c)
            if best:
                c = best[1]
                row = self.tx.pop(c)
                used.add(k); used.add(c)
                t.update(csv=True, amount=row["amount"], _csvCat=row.get("_csvCat", ""), _orderId=row.get("_orderId", ""),
                         detail=("alert %s, posted %s. " % (money(t["amount"]), money(row["amount"]))))
                self.notes["adjusted"].append((k, t["merchant"], row["amount"]))
                continue
            # Inside the statement window but not on the statement: pending or reversed.
            cov = self.coverage.get(t.get("account"))
            if cov and cov[0] <= t["date"] <= (d(cov[1]) - dt.timedelta(days=3)).isoformat():
                t["detail"] = "not on the statement (pending, declined or reversed?)"
                t["suggested"] = "skip"
                self.notes["notOnStatement"].append((k, t["date"], t["merchant"], t["amount"]))

    # -- step 2: bills --------------------------------------------------------------------------
    def apply_bills(self):
        by_gmail = {p["gmailId"]: (pid, p) for pid, p in self.payments.items() if p.get("gmailId")}
        pays = sorted(((p["date"], p.get("amount"), p["billId"]) for p in self.payments.values() if p.get("amount") is not None), key=lambda x: x[0])
        for k, t in self.tx.items():
            if t.get("billId"):
                continue
            hit = None
            if t.get("gmailId") and t["gmailId"] in by_gmail:
                hit = by_gmail[t["gmailId"]][1]["billId"]
            else:
                for pdate, pamt, bid in pays:
                    if pamt is not None and abs(pamt - t["amount"]) < 0.005 and abs(days(pdate, t["date"])) <= 5:
                        b = self.bills.get(bid, {})
                        if b and (overlap(b.get("name", ""), t["merchant"]) or b.get("sender", "") == "" or True):
                            hit = bid
                            break
            if hit and hit in self.bills:
                t["billId"] = hit
                self.notes["billed"].append(k)

    # -- step 3: DoorDash / Uber -------------------------------------------------------------------
    def apply_delivery(self):
        order = {"doordash": 0, "uber": 1, "doordash-order": 2}
        recs = sorted(((rid, r) for rid, r in self.receipts.items() if r["kind"] in order), key=lambda kv: (order[kv[1]["kind"]], kv[1]["date"]))
        used = set(v.get("_rcpt") for v in self.tx.values() if v.get("_rcpt"))
        for rid, r in recs:
            if r.get("txId") and r["txId"] in self.tx and not self.tx[r["txId"]].get("_rcpt"):
                self.tx[r["txId"]]["_rcpt"] = rid   # keep an existing link
            if r.get("total") is None:
                continue
            fam = DD_RE if r["kind"].startswith("doordash") else UBER_RE
            best = None
            for k, t in self.tx.items():
                if t.get("_rcpt") or k in used or not fam.search(t["merchant"]) or t["amount"] <= 0:
                    continue
                gap = days(r["date"], t["date"])
                if gap < -3 or gap > 5:
                    continue
                diff = abs(t["amount"] - r["total"])
                exact = diff < 0.011
                if not exact and diff > max(3.0, 0.15 * r["total"]):
                    continue
                score = (0 if exact else 1, diff, abs(gap))
                if best is None or score < best[0]:
                    best = (score, k, exact)
            if not best:
                self.notes["receiptUnmatched"].append((rid, r["kind"], r["date"], r["merchant"], r["total"]))
                continue
            _, k, exact = best
            t = self.tx[k]
            t["_rcpt"] = rid
            self.receipt_links[rid] = k
            label = r["merchant"] if r["kind"] != "uber" else r["merchant"]
            items = r.get("items") or ("order confirmation only" if r["kind"] == "doordash-order" else r.get("categories", ""))
            t["detail"] = ((t.get("detail") or "") + ("%s: %s" % (label, items)) + ("" if exact else " [amount differs from receipt %s]" % money(r["total"])))[:500]
            if r["kind"].startswith("doordash"):
                t["_cat"] = "delivery"
                t["_sug"] = tag_ok(self.r["doordash"].get(r["merchant"], self.r["categories"].get("delivery", "unnecessary")))
            else:
                eats = r["merchant"] == "Uber Eats"
                t["_cat"] = "delivery" if eats else "rides"
                t["_sug"] = tag_ok(self.r["categories"].get(t["_cat"], ""))

    # -- step 4: Amazon ------------------------------------------------------------------------------
    def apply_amazon(self):
        orders = sorted(((rid, r) for rid, r in self.receipts.items() if r["kind"] == "amazon" and r.get("total")), key=lambda kv: kv[1]["date"])
        refunds = [(rid, r) for rid, r in self.receipts.items() if r["kind"] == "amazon-refund" and r.get("total")]
        amz = {k: t for k, t in self.tx.items() if AMAZON_RE.search(t["merchant"]) and not t.get("billId")}
        # Order numbers: from the statement's "Order Number" line (fresh rows) or an earlier detail text.
        by_order = collections.defaultdict(list)
        for k, t in amz.items():
            oid = t.get("_orderId") or (re.search(r"order ([A-Z0-9]{3}-\d{7}-\d{7})", t.get("detail") or "") or [None, None])[1]
            if oid:
                t["_orderId"] = oid
                by_order[oid].append(k)
        for rid, r in orders:
            if r.get("txId") and r["txId"] in self.tx:
                continue
            keyed = [k for k in by_order.get(r.get("orderId", ""), []) if not amz[k].get("_rcpt")]
            if keyed:
                self.link_amazon(rid, r, keyed, exact_order=True)
                continue
            cands = sorted(((days(r["date"], t["date"]), k) for k, t in amz.items() if not t.get("_rcpt") and t["amount"] > 0 and 0 <= days(r["date"], t["date"]) <= 10 and t["amount"] <= r["total"] + 0.011))
            cands = [k for _, k in cands][:12]
            chosen = None
            for k in cands:
                if abs(amz[k]["amount"] - r["total"]) < 0.011:
                    chosen = [k]
                    break
            if chosen is None:
                for size in range(2, min(5, len(cands)) + 1):
                    for combo in itertools.combinations(cands, size):
                        if abs(sum(amz[k]["amount"] for k in combo) - r["total"]) < 0.011:
                            chosen = list(combo)
                            break
                    if chosen:
                        break
            cands = [k for k in cands if not amz[k].get("_orderId")]   # a charge with its own order number belongs to that order
            if not chosen:
                self.notes["amazonOrderUnmatched"].append((rid, r["date"], r["total"], r.get("categories", "")))
                continue
            self.link_amazon(rid, r, chosen, exact_order=False)
        # Charges whose order number has no email yet still say which order they belong to.
        for k, t in amz.items():
            if t.get("_orderId") and not t.get("_rcpt") and t["amount"] > 0:
                t["_cat"] = "amazon"
                t["detail"] = "order %s: (no order email harvested for this order)" % t["_orderId"]
        for rid, r in refunds:
            best = None
            for k, t in amz.items():
                if t.get("_rcpt") or t["amount"] >= 0 or abs(t["amount"] - r["total"]) > 0.011:
                    continue
                gap = days(r["date"], t["date"])
                if -3 <= gap <= 12 and (best is None or abs(gap) < best[0]):
                    best = (abs(gap), k)
            if best:
                t = amz[best[1]]
                t["_rcpt"] = rid; t["_cat"] = "amazon"
                t["detail"] = ("refund: %s (order %s)" % (r.get("items") or "item", r.get("orderId", "")))[:500]
                self.receipt_links[rid] = best[1]
        for k, t in amz.items():
            if not t.get("_rcpt") and t["amount"] > 0 and not t.get("detail"):
                t["detail"] = "no order email matched (digital, Prime Video, Subscribe & Save, Whole Foods or gift card?)"
                self.notes["amazonChargeUnmatched"].append((k, t["date"], t["amount"], t["merchant"]))

    def delivery_default(self, merchant):
        """DD *DOORDASH JEWEL-OSC → the rules.doordash entry for Jewel-Osco, if any."""
        m = re.sub(r"^(DD \*DOORDASH|DOORDASH\*?|UBER \*EATS)\s*", "", merchant, flags=re.I)
        key = re.sub(r"[^a-z0-9]", "", m.split(" 8")[0].lower())[:8]
        if not key:
            return ""
        for name, tag in self.r["doordash"].items():
            n = re.sub(r"[^a-z0-9]", "", name.lower())
            if n.startswith(key) or key.startswith(n[:len(key)]):
                return tag_ok(tag)
        return ""

    def amazon_suggestion(self, r):
        first_cat = (r.get("categories") or "").split(",")[0].strip()
        sug = tag_ok(self.r["amazonCategory"].get(first_cat, ""))
        if not sug and r.get("items"):
            low = r["items"].lower()
            for tag, words in self.r.get("amazonItems", {}).items():
                if any(w in low for w in words):
                    return tag_ok(tag)
        return sug

    def link_amazon(self, rid, r, chosen, exact_order):
        sug = self.amazon_suggestion(r)
        charges = [k for k in chosen if self.tx[k]["amount"] > 0]
        for k in chosen:
            t = self.tx[k]
            t["_rcpt"] = rid
            t["_cat"] = "amazon"
            t["_sug"] = sug
            what = (r.get("categories") or r["merchant"]) + ((" — " + r["items"]) if r.get("items") else "")
            if t["amount"] <= 0:
                t["detail"] = ("refund on order %s: %s" % (r.get("orderId", ""), what))[:500]
            else:
                t["detail"] = ("order %s: %s (%d of %d charges%s)" % (r.get("orderId", ""), what, charges.index(k) + 1, len(charges),
                                                                     "" if exact_order else ", matched by amount"))[:500]
        self.receipt_links[rid] = chosen[0]

    # -- step 5: categories + suggestions ----------------------------------------------------------
    def finalize(self):
        for k, t in self.tx.items():
            rcat, rsug = rule_for(t["merchant"], self.r)
            bill = self.bills.get(t.get("billId") or "", {})
            bcat = (bill.get("category") or "").lower()
            ccat = self.r["csvCategories"].get((t.pop("_csvCat", "") or "").lower(), "")
            cat = t.pop("_cat", "") or bcat or rcat or ccat or ""
            if not t.get("category"):
                t["category"] = cat
            sug = t.pop("_sug", "")
            if not sug and cat == "delivery" and not t.get("detail"):
                sug = self.delivery_default(t["merchant"])
            if not sug:
                if bill:
                    sug = tag_ok(self.r["billCategories"].get(bcat, ""))
                sug = sug or rsug or tag_ok(self.r["categories"].get(t["category"], ""))
            if t.get("suggested") in ("skip",) and t.get("detail", "").startswith("not on the statement"):
                sug = "skip"
            if t["amount"] < 0 and t["category"] not in ("income", "transfer"):
                sug = sug or ""  # credits net against their category; no nag
            t["suggested"] = "" if t.get("tag") else tag_ok(sug)   # a tagged row needs no suggestion
            t.pop("_rcpt", None); t.pop("_status", None); t.pop("_orderId", None)
            t.setdefault("tag", ""); t.setdefault("note", ""); t.setdefault("detail", ""); t.setdefault("billId", ""); t.setdefault("gmailId", "")

    def run(self):
        self.apply_csv()
        self.apply_bills()
        self.apply_delivery()
        self.apply_amazon()
        self.finalize()
        return self.tx, self.receipt_links, self.notes


def diff_rows(proposed, current):
    """Rows to push: new ids, and existing ids whose non-owned fields changed (category only when the Sheet's is empty)."""
    out = {}
    for k, t in proposed.items():
        cur = current.get(k)
        if cur is None:
            out[k] = t
            continue
        changed = any(norm_val(t.get(f)) != norm_val(cur.get(f)) for f in NON_OWNED)
        if not cur.get("category") and t.get("category"):
            changed = True
        if changed:
            out[k] = t
    return out


def norm_val(x):
    if isinstance(x, float):
        return round(x, 2)
    if x is None:
        return ""
    return x


def cmd_match(_args):
    guard_repo()
    sheet = load_cache("sheet.json")
    csvdata = load_cache("csv.json", required=False)
    if not csvdata:
        print("no csv.json yet (run ingest); matching alerts and receipts only")
    r = rules()
    tx, links, notes = Matcher(sheet, csvdata, r).run()
    to_push = diff_rows(tx, live(sheet["transactions"]))
    receipts = {}
    for rid, k in links.items():
        rc = sheet["receipts"].get(rid)
        if rc and rc.get("txId") != k:
            receipts[rid] = dict(rc, txId=k)
    budget_seed = {}
    cats = collections.Counter(t["category"] for t in tx.values() if t["category"])
    for c in cats:
        if c not in live(sheet["budget"]) and c not in ("income", "transfer"):
            budget_seed[c] = {"target": None, "note": "set a monthly target"}
    out = {"transactions": tx, "push": to_push, "receipts": receipts, "budget": budget_seed, "notes": dict(notes), "matched": now_iso()}
    p = save_cache("proposed.json", out)
    n = len(tx)
    print("%d transactions (%d new/changed to push), %d receipt links, %d budget rows to seed → %s" % (n, len(to_push), len(receipts), len(budget_seed), os.path.relpath(p, ROOT)))
    print("  untagged spend rows: %d | with a suggestion: %d | unmatched Amazon charges: %d | unmatched Amazon orders: %d | unmatched delivery receipts: %d | alerts not on statement: %d | adjusted: %d"
          % (sum(1 for t in tx.values() if t["amount"] > 0 and not t.get("tag")), sum(1 for t in tx.values() if t["amount"] > 0 and not t.get("tag") and t.get("suggested")),
             len(notes.get("amazonChargeUnmatched", [])), len(notes.get("amazonOrderUnmatched", [])), len(notes.get("receiptUnmatched", [])),
             len(notes.get("notOnStatement", [])), len(notes.get("adjusted", []))))
    return out


def cmd_push(args):
    guard_repo()
    prop = load_cache("proposed.json")
    stamp = now_iso()
    rows = {k: dict(v, u=stamp) for k, v in prop["push"].items()}
    for row in rows.values():
        row["tag"] = ""      # never pushed: the Sheet owns them (a filled cell survives, an empty one stays empty)
        row["note"] = ""
    receipts = {k: dict(v, u=stamp) for k, v in prop["receipts"].items()}
    budget = {k: dict(v, u=stamp) for k, v in prop["budget"].items()}
    print("push: %d transactions, %d receipt links, %d budget seeds%s" % (len(rows), len(receipts), len(budget), " (DRY)" if args.dry else ""))
    if args.dry:
        for k, v in list(rows.items())[:15]:
            print("  %s %s %-32s %10s %-12s sug=%-11s %s" % (k, v["date"], v["merchant"][:32], money(v["amount"]), v["category"], v["suggested"], v["detail"][:60]))
        return
    ids = list(rows)
    for i in range(0, len(ids), BATCH):
        chunk = {k: rows[k] for k in ids[i:i + BATCH]}
        j = sheet_post({"transactions": chunk})
        print("  transactions %d-%d ok (%s rows in Sheet)" % (i + 1, i + len(chunk), j.get("transactions", {}).get("rows")))
    if receipts:
        rids = list(receipts)
        for i in range(0, len(rids), BATCH):
            sheet_post({"receipts": {k: receipts[k] for k in rids[i:i + BATCH]}})
        print("  receipts linked")
    if budget:
        sheet_post({"budget": budget})
        print("  budget rows seeded: %s" % ", ".join(sorted(budget)))
    cmd_pull(args)


def cmd_accept_suggestions(args):
    """Move every suggestion into the tag column (rows with no tag yet) and clear the suggestion."""
    guard_repo()
    sheet = cmd_pull(args)
    tx = live(sheet["transactions"])
    stamp = now_iso()
    rows = {}
    for k, t in tx.items():
        if not t.get("tag") and t.get("suggested") in TAGS:
            rows[k] = dict(t, tag=t["suggested"], suggested="", u=stamp)
    by_tag = collections.Counter(r["tag"] for r in rows.values())
    print("accept: %d rows → %s%s" % (len(rows), dict(by_tag), " (DRY)" if args.dry else ""))
    if args.dry or not rows:
        return
    ids = list(rows)
    for i in range(0, len(ids), BATCH):
        sheet_post({"transactions": {k: rows[k] for k in ids[i:i + BATCH]}})
        print("  %d-%d written" % (i + 1, min(i + BATCH, len(ids))))
    cmd_pull(args)


def cmd_tag(args):
    """Fill the tag on every untagged row in the given categories (filled tags are the Sheet's and stay)."""
    guard_repo()
    if args.tag not in TAGS:
        die("tag must be one of " + ", ".join(TAGS))
    cats = {c.strip().lower() for c in (args.category or "").split(",") if c.strip()}
    merchant = re.compile(args.merchant, re.I) if args.merchant else None
    if not cats and not merchant:
        die("give --category and/or --merchant")
    sheet = cmd_pull(args)
    tx = live(sheet["transactions"])
    stamp = now_iso()
    rows, kept = {}, collections.Counter()
    for k, t in tx.items():
        if cats and (t.get("category") or "") not in cats:
            continue
        if merchant and not merchant.search(t.get("merchant") or ""):
            continue
        if t.get("tag"):
            kept[t["tag"]] += 1
            continue
        rows[k] = dict(t, tag=args.tag, suggested="", u=stamp)
    scope = ",".join(sorted(cats)) + ((" merchant~/%s/" % args.merchant) if merchant else "")
    print("tag %s on %s: %d rows to fill, already tagged and left alone: %s%s" % (args.tag, scope, len(rows), dict(kept) or "none", " (DRY)" if args.dry else ""))
    if args.dry or not rows:
        return
    ids = list(rows)
    for i in range(0, len(ids), BATCH):
        sheet_post({"transactions": {k: rows[k] for k in ids[i:i + BATCH]}})
    cmd_pull(args)


def cmd_pull(_args):
    guard_repo()
    j = sheet_get()
    p = save_cache("sheet.json", j)
    print("pulled v%s: %d bills, %d payments, %d transactions, %d receipts, %d budget rows → %s (last email scan %s)"
          % (j.get("version"), len(live(j["bills"])), len(live(j["payments"])), len(live(j["transactions"])), len(live(j["receipts"])), len(live(j["budget"])),
             os.path.relpath(p, ROOT), j.get("lastBudgetScan") or "never"))
    return j


# ------------------------------------------------------------------------------------ report

def month_of(date):
    return date[:7]


def monthly_from_bill(b, payments):
    amts = sorted((p["amount"] for p in payments if p.get("billId") == b.get("_id") and p.get("amount")), key=lambda x: x)
    typical = statistics.median(amts[-6:]) if amts else (b.get("amount") or 0)
    per = {"weekly": 52 / 12, "monthly": 1, "bimonthly": 0.5, "quarterly": 1 / 3, "semiannual": 1 / 6, "annual": 1 / 12}.get(b.get("cadence"), 1)
    return typical * per


def cmd_report(args):
    guard_repo()
    sheet = load_cache("sheet.json")
    r = rules()
    tx = live(sheet["transactions"])
    # Rows matched locally but not pushed yet (or a Sheet that is still empty) still count.
    prop = load_cache("proposed.json", required=False)
    if prop:
        for k, t in prop["transactions"].items():
            if k not in tx and not t.get("del"):
                tx[k] = t
    receipts = live(sheet["receipts"])
    bills = live(sheet["bills"])
    payments = live(sheet["payments"])
    targets = live(sheet["budget"])
    today = dt.date.today()
    months = []
    m = today.replace(day=1)
    for _ in range(args.months):
        m = (m - dt.timedelta(days=1)).replace(day=1)
        months.append(m.isoformat()[:7])
    months.reverse()
    cur_month = today.isoformat()[:7]
    inwin = {k: t for k, t in tx.items() if month_of(t["date"]) in months}
    partial = {k: t for k, t in tx.items() if month_of(t["date"]) == cur_month}

    def eff(t):
        return t.get("tag") or t.get("suggested") or ""

    def spend_rows(rows):
        return {k: t for k, t in rows.items() if t.get("category") not in ("income", "transfer") and eff(t) != "skip"}

    spend = spend_rows(inwin)
    income = {k: t for k, t in inwin.items() if t.get("category") == "income"}
    transfers = {k: t for k, t in inwin.items() if t.get("category") == "transfer"}

    def by_month(rows, key=lambda t: True):
        out = collections.defaultdict(float)
        for t in rows.values():
            if key(t):
                out[month_of(t["date"])] += t["amount"]
        return out

    def table(headers, rows):
        lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---:" if i else "---" for i in range(len(headers))) + "|"]
        for row in rows:
            lines.append("| " + " | ".join(str(c) for c in row) + " |")
        return "\n".join(lines)

    L = []
    L.append("# Spending breakdown and budget — %s to %s\n" % (months[0], months[-1]))
    L.append("Generated %s from the Home Logbook Sheet (%d transactions in window, %d this month so far). "
             "Rows use the Sheet's **tag** when set, otherwise the **suggested** tag; `skip`, transfers and income are excluded from spend.\n"
             % (today.isoformat(), len(inwin), len(partial)))
    untagged = [t for t in spend.values() if t["amount"] > 0 and not t.get("tag")]
    L.append("**Tagging status:** %d of %d spend rows still untagged (%d of those have a suggestion). Tag them in the Sheet's `transactions` tab.\n"
             % (len(untagged), sum(1 for t in spend.values() if t["amount"] > 0), sum(1 for t in untagged if t.get("suggested"))))

    # -- headline
    inc_m = by_month(income)
    sp_m = by_month(spend)
    tr_out = by_month(transfers, lambda t: t["amount"] > 0)
    tr_in = by_month(transfers, lambda t: t["amount"] < 0)
    n = len(months)
    L.append("## Headline (monthly averages over %d months)\n" % n)
    L.append(table(["", "per month", "total"], [
        ["Income (paychecks, support, reimbursements)", money(-sum(inc_m.values()) / n), money(-sum(inc_m.values()))],
        ["Spend (all non-skip rows)", money(sum(sp_m.values()) / n), money(sum(sp_m.values()))],
        ["Transfers out (card payments are excluded; savings/other accounts)", money((sum(tr_out.values()) + sum(tr_in.values())) / n), money(sum(tr_out.values()) + sum(tr_in.values()))],
    ]))
    L.append("\nCard payments to Chase are not counted as spend: the Chase purchases themselves are. If the Chase CSV is not loaded yet, the spend line is missing most card spending.\n")

    # -- by tag
    L.append("## Necessary / unnecessary / frivolous\n")
    rows = []
    for tag in ("necessary", "unnecessary", "frivolous", ""):
        mm = by_month(spend, lambda t, tag=tag: eff(t) == tag)
        rows.append([tag or "(untagged, no suggestion)", money(sum(mm.values()) / n), money(sum(mm.values()))] + [money(mm.get(mo, 0)) for mo in months])
    L.append(table(["tag", "avg/mo", "total"] + months, rows))
    L.append("")

    # -- by category
    L.append("## Spend by category and month\n")
    cats = collections.defaultdict(lambda: collections.defaultdict(float))
    for t in spend.values():
        cats[t.get("category") or "(uncategorised)"][month_of(t["date"])] += t["amount"]
    rows = []
    for c, mm in sorted(cats.items(), key=lambda kv: -sum(kv[1].values())):
        tot = sum(mm.values())
        rows.append([c, money(tot / n), money(tot)] + [money(mm.get(mo, 0)) if mm.get(mo) else "" for mo in months])
    L.append(table(["category", "avg/mo", "total"] + months, rows))
    L.append("")

    # -- Amazon
    amz = {k: t for k, t in spend.items() if t.get("category") == "amazon"}
    L.append("## Amazon\n")
    if amz:
        charges = [t for t in amz.values() if t["amount"] > 0]
        credits = [t for t in amz.values() if t["amount"] < 0]
        matched = [t for t in charges if t.get("detail", "").startswith("order ") and "(no order email" not in t["detail"]]
        numbered = [t for t in charges if "(no order email" in t.get("detail", "")]
        L.append("%d charges totalling %s (%s/month), %d refunds totalling %s. %d charges matched to an order email, %d carry an order number but its email is not harvested yet, %d have neither.\n"
                 % (len(charges), money(sum(t["amount"] for t in charges)), money(sum(t["amount"] for t in charges) / n), len(credits), money(-sum(t["amount"] for t in credits)),
                    len(matched), len(numbered), len(charges) - len(matched) - len(numbered)))
        mm = by_month(amz)
        L.append(table(["month"] + months, [["Amazon net"] + [money(mm.get(mo, 0)) for mo in months]]))
        L.append("")
        bycat = collections.defaultdict(lambda: [0, 0.0])
        for t in matched:
            c = re.sub(r"^order \S+: ", "", t["detail"]).split(" (")[0].split(" — ")[0]
            bycat[c][0] += 1; bycat[c][1] += t["amount"]
        L.append("**By Amazon's own category words** (from the order emails):\n")
        L.append(table(["categories", "charges", "total", "tag now"], [[c, v[0], money(v[1]), collections.Counter(eff(t) or "untagged" for t in matched if t["detail"].find(c) > 0).most_common(1)[0][0]] for c, v in sorted(bycat.items(), key=lambda kv: -kv[1][1])]))
        L.append("")
        tagc = collections.Counter(eff(t) or "untagged" for t in charges)
        L.append("Tags on Amazon charges: " + ", ".join("%s %d" % (k, v) for k, v in tagc.most_common()) + ".\n")
        unm = sorted((t for t in charges if t not in matched and t not in numbered), key=lambda t: -t["amount"])[:20]
        if unm:
            L.append("Largest Amazon charges with no matching order email (digital, Prime Video, Subscribe & Save, Whole Foods, or the order email is older than the harvest window):\n")
            L.append(table(["date", "amount", "merchant"], [[t["date"], money(t["amount"]), t["merchant"]] for t in unm]))
            L.append("")
    else:
        L.append("No Amazon rows in the window yet (load the Chase CSV and run the backfill).\n")

    # -- delivery
    dlv = {k: t for k, t in spend.items() if t.get("category") in ("delivery", "rides") or DD_RE.search(t["merchant"]) or UBER_RE.search(t["merchant"])}
    L.append("## DoorDash, Uber Eats and rides\n")
    if dlv:
        orders = [t for t in dlv.values() if t["amount"] > 0]
        L.append("%d charges totalling %s (%s/month, average %s per order).\n" % (len(orders), money(sum(t["amount"] for t in orders)), money(sum(t["amount"] for t in orders) / n),
                                                                                money(sum(t["amount"] for t in orders) / max(1, len(orders)))))
        mm = by_month(dlv)
        cnt = collections.Counter(month_of(t["date"]) for t in orders)
        L.append(table(["month"] + months, [["delivery + rides"] + [money(mm.get(mo, 0)) for mo in months], ["orders"] + [cnt.get(mo, 0) for mo in months]]))
        L.append("")
        bym = collections.defaultdict(lambda: [0, 0.0])
        for t in orders:
            mname = t["detail"].split(":")[0] if t.get("detail") and not t["detail"].startswith(("alert", "no ", "not ")) else re.sub(r"^DD \*DOORDASH ?", "DoorDash ", t["merchant"])
            bym[mname[:30]][0] += 1; bym[mname[:30]][1] += t["amount"]
        L.append(table(["merchant", "orders", "total", "avg"], [[m_, v[0], money(v[1]), money(v[1] / v[0])] for m_, v in sorted(bym.items(), key=lambda kv: -kv[1][1])[:25]]))
        L.append("")
        tagc = collections.Counter(eff(t) or "untagged" for t in orders)
        L.append("Tags on delivery/ride charges: " + ", ".join("%s %d" % (k, v) for k, v in tagc.most_common()) + ".\n")
    else:
        L.append("No delivery rows in the window yet.\n")

    # -- fixed bills
    L.append("## Fixed bills (from the bills tab, normalised to a month)\n")
    rows, fixed_total = [], 0.0
    for bid, b in sorted(bills.items(), key=lambda kv: kv[1].get("category", "")):
        if b.get("active") is False:
            continue
        b["_id"] = bid
        per = monthly_from_bill(b, payments.values())
        fixed_total += per
        rows.append([b.get("name", bid), b.get("category", ""), b.get("cadence", ""), money(per), r["billCategories"].get((b.get("category") or "").lower(), "") or "—"])
    rows.append(["**Total fixed**", "", "", "**%s**" % money(fixed_total), ""])
    L.append(table(["bill", "category", "cadence", "per month", "default tag"], rows))
    L.append("")

    # -- proposed budget
    L.append("## Proposed monthly budget\n")
    L.append("12-month average, median of the last 6 months, the necessary-only average, and the target from the Sheet's `budget` tab (edit it there).\n")
    rows = []
    last6 = months[-6:]
    tot = [0.0, 0.0, 0.0, 0.0]
    for c, mm in sorted(cats.items(), key=lambda kv: -sum(kv[1].values())):
        avg = sum(mm.values()) / n
        med6 = statistics.median([mm.get(mo, 0.0) for mo in last6]) if last6 else 0
        nec = sum(t["amount"] for t in spend.values() if (t.get("category") or "(uncategorised)") == c and eff(t) == "necessary") / n
        tgt = targets.get(c, {}).get("target")
        gap = (tgt - avg) if tgt is not None else None
        tot[0] += avg; tot[1] += med6; tot[2] += nec; tot[3] += (tgt or 0)
        rows.append([c, money(avg), money(med6), money(nec), money(tgt) if tgt is not None else "", (money(gap) if gap is not None else "")])
    rows.append(["**Total**", "**%s**" % money(tot[0]), "**%s**" % money(tot[1]), "**%s**" % money(tot[2]), "**%s**" % money(tot[3]), ""])
    L.append(table(["category", "12-mo avg", "last-6 median", "necessary only", "target", "target − avg"], rows))
    L.append("")
    inc_avg = -sum(inc_m.values()) / n
    L.append("Income averages %s a month; spend %s; the difference (%s) is what transfers to savings, debt or slack can absorb. "
             "A first budget that keeps every *necessary* row and halves *unnecessary* and *frivolous* would be about **%s/month**.\n"
             % (money(inc_avg), money(tot[0]), money(inc_avg - tot[0]),
                money(tot[2] + 0.5 * sum(t["amount"] for t in spend.values() if eff(t) in ("unnecessary", "frivolous")) / n)))

    # -- open items
    L.append("## Open items\n")
    pend = [t for t in tx.values() if t.get("detail", "").startswith("not on the statement")]
    if pend:
        L.append("- %d alert rows are not on any statement line (pending, declined or reversed); they are suggested `skip`." % len(pend))
    adj = [t for t in tx.values() if t.get("detail", "").startswith("alert ")]
    if adj:
        L.append("- %d alerts posted at a different amount (DoorDash adjustments/tips); the posted amount is used." % len(adj))
    L.append("- %d Amazon charges without an order email; %d receipts without a charge." % (
        sum(1 for t in amz.values() if t["amount"] > 0 and t.get("detail", "").startswith("no order")),
        sum(1 for rc in receipts.values() if rc["kind"] in ("amazon", "doordash", "uber") and not rc.get("txId"))))
    uncat = [t for t in spend.values() if not t.get("category")]
    if uncat:
        top = collections.Counter(t["merchant"] for t in uncat).most_common(15)
        L.append("- %d spend rows have no category. Most frequent merchants: %s. Add rules to budget/rules.json or type the category in the Sheet." % (len(uncat), ", ".join("%s (%d)" % kv for kv in top)))
    L.append("")
    open(REPORT, "w").write("\n".join(L))
    print("wrote %s (%d months, %d spend rows, %d untagged)" % (os.path.relpath(REPORT, ROOT), n, len(spend), len(untagged)))


def cmd_run(args):
    cmd_pull(args)
    if any(f.lower().endswith((".csv", ".pdf")) for f in os.listdir(STATEMENTS)):
        cmd_ingest(args)
    cmd_match(args)
    cmd_push(args)
    cmd_report(args)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pull")
    b = sub.add_parser("backfill"); b.add_argument("--months", type=int, default=12); b.add_argument("--kind", default="all"); b.add_argument("--dry", action="store_true")
    sub.add_parser("ingest")
    sub.add_parser("match")
    p = sub.add_parser("push"); p.add_argument("--dry", action="store_true")
    rp = sub.add_parser("report"); rp.add_argument("--months", type=int, default=12)
    rs = sub.add_parser("rescan-receipts"); rs.add_argument("--dry", action="store_true")
    ac = sub.add_parser("accept-suggestions"); ac.add_argument("--dry", action="store_true")
    tg = sub.add_parser("tag"); tg.add_argument("--category"); tg.add_argument("--merchant", help="regex on the merchant text"); tg.add_argument("--tag", required=True); tg.add_argument("--dry", action="store_true")
    rn = sub.add_parser("run"); rn.add_argument("--months", type=int, default=12); rn.add_argument("--dry", action="store_true")
    args = ap.parse_args(argv)
    guard_repo()
    {"pull": cmd_pull, "backfill": cmd_backfill, "ingest": cmd_ingest, "match": cmd_match, "push": cmd_push, "report": cmd_report, "run": cmd_run,
     "rescan-receipts": cmd_rescan_receipts, "accept-suggestions": cmd_accept_suggestions, "tag": cmd_tag}[args.cmd](args)


if __name__ == "__main__":
    main()
