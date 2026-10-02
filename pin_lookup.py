"""PIN code lookup engine.

Primary source: the free postalpincode.in API (no key needed) -- complete,
up-to-date India Post data.
Fallback: offline SQLite FTS index built from the IndiaPost/pin dataset
(data/build_db.py), used when the API is unreachable.

Given a free-text Indian address, returns the most likely PIN code(s).
"""
import difflib
import json
import re
import sqlite3
import subprocess
import urllib.parse
import urllib.request

API_URL = "https://api.postalpincode.in/postoffice/{}"
API_TIMEOUT = 15

# Words that carry almost no location signal (kept OUT of stopwords:
# street/road/lane/nagar and single letters, which are part of place names).
STOPWORDS = {
    "no", "plot", "flat", "house", "building", "floor",
    "near", "opp", "opposite", "behind", "above", "at", "the", "and",
    "of", "india", "address", "addr", "pin", "code", "pincode",
}

# Name variants -> the form used in postal data.
ALIASES = {
    "bengaluru": "bangalore",
    "mysuru": "mysore",
    "prayagraj": "allahabad",
    "calcutta": "kolkata",
    "bombay": "mumbai",
    "madras": "chennai",
}

# Multi-word states/districts, matched as phrases BEFORE tokenising
# (so "west" in "West Bengal" isn't mistaken for a direction, etc).
MULTIWORD_STATES = [
    "andhra pradesh", "arunachal pradesh", "himachal pradesh",
    "madhya pradesh", "uttar pradesh", "west bengal", "tamil nadu",
    "jammu and kashmir", "andaman and nicobar islands",
    "dadra and nagar haveli", "daman and diu", "dadar and nagar haveli",
]
MULTIWORD_DISTRICTS = [
    "gautam buddha nagar", "gautam budh nagar",
]

# City name -> district string used in postal data (for cities whose
# district name differs, or that are missing from the offline DB).
CITY_DISTRICTS = {
    "noida": "gautam buddha nagar",
    "gurgaon": "gurgaon",
    "gurugram": "gurgaon",
    "bangalore": "bangalore",
    "mumbai": "mumbai",
    "delhi": "delhi",
    "kolkata": "kolkata",
    "chennai": "chennai",
    "hyderabad": "hyderabad",
    "pune": "pune",
    "ahmedabad": "ahmedabad",
    "jaipur": "jaipur",
    "lucknow": "lucknow",
    "kanpur": "kanpur",
    "nagpur": "nagpur",
    "indore": "indore",
    "thane": "thane",
    "bhopal": "bhopal",
    "patna": "patna",
    "kochi": "ernakulam",
    "cochin": "ernakulam",
}

STATES = {
    "andhra pradesh", "arunachal pradesh", "assam", "bihar",
    "chhattisgarh", "goa", "gujarat", "haryana", "himachal pradesh",
    "jharkhand", "karnataka", "kerala", "madhya pradesh", "maharashtra",
    "manipur", "meghalaya", "mizoram", "nagaland", "odisha", "orissa",
    "punjab", "rajasthan", "sikkim", "tamil nadu", "telangana", "tripura",
    "uttar pradesh", "uttarakhand", "west bengal",
    "andaman and nicobar", "chandigarh", "delhi", "jammu and kashmir",
    "ladakh", "lakshadweep", "puducherry", "pondicherry", "daman and diu",
    "dadra and nagar haveli",
}

DIRECTIONS = {"east", "west", "north", "south"}

_DB_SETS = {}


def _load_sets(db_path):
    """Load known district/state spellings from the offline DB."""
    if db_path in _DB_SETS:
        return _DB_SETS[db_path]
    sets = {"districts": set(), "states": set()}
    try:
        con = sqlite3.connect(db_path)
        for (d,) in con.execute("SELECT DISTINCT district FROM offices"):
            if d:
                sets["districts"].add(_norm(d))
        for (s,) in con.execute("SELECT DISTINCT state FROM offices"):
            if s:
                sets["states"].add(_norm(s))
        con.close()
    except Exception:
        pass
    _DB_SETS[db_path] = sets
    return sets


def _norm(text):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def significant_tokens(text):
    toks = [t for t in _norm(text).split() if t not in STOPWORDS]
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return [ALIASES.get(t, t) for t in out]


# Famous localities whose official post-office name differs a lot
# from the common name (applied to the search phrase).
LOCALITY_ALIASES = {
    "t nagar": "thygarayanagar",
}


def _split_locality(text, db_path):
    """Separate locality tokens from district/state keys.

    Multi-word states/districts are pulled out as phrases first, then the
    remainder is tokenised. Returns (locality, district_keys, state_keys);
    district_keys are (token, district_string) pairs for substring matching.
    """
    sets = _load_sets(db_path)
    norm = _norm(text)
    district_keys, state_keys = [], []
    for phrase in MULTIWORD_STATES:
        if phrase in norm:
            state_keys.append(phrase)
            norm = norm.replace(phrase, " ")
    for phrase in MULTIWORD_DISTRICTS:
        if phrase in norm:
            district_keys.append((phrase, phrase))
            norm = norm.replace(phrase, " ")
    locality = []
    for t in significant_tokens(norm):
        if t in STATES or t in sets["states"]:
            state_keys.append(t)
        elif t in sets["districts"] or t in CITY_DISTRICTS:
            district_keys.append((t, CITY_DISTRICTS.get(t, t)))
        else:
            locality.append(t)
    return locality, district_keys, state_keys


def _api_search(name):
    try:
        url = API_URL.format(urllib.parse.quote(name))
        req = urllib.request.Request(url, headers={"User-Agent": "pin-bot/1.0"})
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None  # network/API failure -> caller falls back
    if not data or data[0].get("Status") != "Success":
        return []
    return data[0].get("PostOffice") or []


def _score_office(office, locality_phrase, locality_tokens,
                  district_keys, state_keys):
    name = _norm(office.get("Name", ""))
    name_toks = set(name.split())
    lt = set(locality_tokens)

    ratio = difflib.SequenceMatcher(None, locality_phrase, name).ratio()
    overlap = len(lt & name_toks) / max(len(lt), 1)

    score = 5 * ratio + 4 * overlap

    office_district = _norm(office.get("District", ""))
    office_state = _norm(office.get("State", ""))

    def district_hit():
        return any(tok in office_district or ds in office_district
                   for tok, ds in district_keys)

    def state_hit():
        return any(s in office_state for s in state_keys)

    d_hit, s_hit = district_hit(), state_hit()
    if d_hit:
        score += 3
    if s_hit:
        score += 2
    if d_hit and s_hit:
        score += 2
    # Penalise when the address names a district/state the office lacks.
    if district_keys and not d_hit:
        score -= 4
    if state_keys and not s_hit:
        score -= 3

    # Penalise direction mismatch (Andheri East vs "Andheri West").
    addr_dirs = lt & DIRECTIONS
    name_dirs = name_toks & DIRECTIONS
    if addr_dirs and name_dirs and not (addr_dirs & name_dirs):
        score -= 5

    return round(score, 2)


def _collect(queries, phrase, locality, district_keys, state_keys):
    """Run API queries, score every office, return sorted (score, office)."""
    seen, scored = set(), []
    for q in queries:
        offices = _api_search(q)
        if offices is None:
            return None  # network failure signal
        for o in offices:
            key = (o.get("Name"), o.get("Pincode"))
            if key in seen:
                continue
            seen.add(key)
            scored.append((_score_office(o, phrase, locality,
                                         district_keys, state_keys), o))
        if scored and max(s for s, _ in scored) >= 9:
            break  # strong hit; no need for more queries
    scored.sort(key=lambda x: -x[0])
    return scored


def _from_api(address, db_path):
    locality, district_keys, state_keys = _split_locality(address, db_path)
    if not locality and not district_keys:
        locality = significant_tokens(address)
    if not locality and not district_keys:
        return []

    phrase = " ".join(locality)
    phrase = LOCALITY_ALIASES.get(phrase, phrase)
    scored = _collect([phrase] + locality, phrase, locality,
                      district_keys, state_keys)
    if scored is None:
        return None

    # Fallback: search by district and take the head post office, so that
    # "MG Road, Bengaluru" still resolves to the area's main PIN (560001)
    # when no office is named after the street itself.
    confident_best = max([s for s, _ in scored], default=-99) >= 6
    if not confident_best and district_keys:
        for _, ds in district_keys:
            extra = _collect([ds], ds, [ds], district_keys, state_keys)
            if extra is None:
                return None
            # Prefer the head office: its name matches the district itself
            # (e.g. "Bangalore" -> 560001), then GPO/head/general offices.
            def _fb_key(item):
                s, o = item
                nm = _norm(o.get("Name", ""))
                return (0 if nm == ds else
                        1 if any(k in nm for k in ("gpo", "head", "general"))
                        else 2, -s)
            extra.sort(key=_fb_key)
            scored = extra + scored
            break

    out, seen_pin = [], set()
    for s, o in scored:
        if o["Pincode"] in seen_pin:
            continue
        seen_pin.add(o["Pincode"])
        out.append({
            "pincode": o["Pincode"],
            "officename": o.get("Name", "").strip(),
            "taluk": o.get("Block", ""),
            "district": o.get("District", ""),
            "state": o.get("State", ""),
            "score": s,
            "confident": s >= 6,
        })
        if len(out) >= 3:
            break
    return out


def _from_offline_db(address, db_path):
    """Offline fallback using the local FTS index."""
    tokens = significant_tokens(address)
    if not tokens:
        return []
    try:
        con = sqlite3.connect(db_path)
    except Exception:
        return []
    con.row_factory = sqlite3.Row
    try:
        q = " OR ".join(f'"{t}"' for t in tokens)
        cur = con.execute(
            "SELECT o.* FROM offices_fts JOIN offices o ON o.id = offices_fts.rowid"
            " WHERE offices_fts MATCH ? LIMIT 40",
            (f"{{officename taluk district state}} : ({q})",),
        )
        rows = cur.fetchall()
    except Exception:
        rows = []
    finally:
        con.close()
    tset = set(tokens)
    for a, b in ALIASES.items():
        if a in tset:
            tset.add(b)

    def overlap(field, w):
        return w * len(tset & (set(_norm(field).split()) - STOPWORDS))

    scored = []
    for r in rows:
        d = dict(r)
        s = (overlap(d["officename"], 3) + overlap(d["taluk"], 2)
             + overlap(d["district"], 3) + overlap(d["state"], 2))
        if s > 0:
            scored.append((s, d))
    scored.sort(key=lambda x: (-x[0], x[1]["pincode"]))
    out, seen = [], set()
    for s, d in scored:
        if d["pincode"] in seen:
            continue
        seen.add(d["pincode"])
        out.append({"pincode": d["pincode"], "officename": d["officename"],
                    "taluk": d["taluk"], "district": d["district"],
                    "state": d["state"], "score": round(s, 2),
                    "confident": s >= 6})
        if len(out) >= 3:
            break
    return out


def find_pincodes(address, db_path="data/pincodes.db", limit=3):
    """Best-effort PIN lookup. Returns up to `limit` candidate dicts."""
    cands = _from_api(address, db_path)
    if cands is None:  # API unreachable -> offline fallback
        cands = _from_offline_db(address, db_path)
    return (cands or [])[:limit]


def split_addresses(message):
    """Split one message into individual addresses (newlines / numbered lists)."""
    lines = []
    for raw in message.splitlines():
        line = re.sub(r"^[\s\-\*\•\d]+[\.\)\:\-]\s*", "", raw).strip()
        if len(line) >= 4:
            lines.append(line)
    return lines or ([message.strip()] if message.strip() else [])


def ocr_image(image_path):
    """Extract text from an address photo using Tesseract OCR."""
    try:
        out = subprocess.run(
            ["tesseract", image_path, "stdout", "-l", "eng"],
            capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise RuntimeError(
            "Tesseract OCR is not installed. On the server run: "
            "sudo apt-get install tesseract-ocr")
    text = out.stdout.strip()
    if not text:
        raise RuntimeError("No text found in the image.")
    return text


def lookup_message(message, db_path="data/pincodes.db"):
    return [(a, find_pincodes(a, db_path)) for a in split_addresses(message)]


def format_results(results):
    if not results:
        return ("I couldn't find any address in your message. "
                "Send addresses as text or a photo.")
    parts = []
    for i, (addr, cands) in enumerate(results, 1):
        short = addr if len(addr) <= 60 else addr[:57] + "..."
        if not cands:
            parts.append(f"{i}. {short}\n   PIN not found — try adding area / district / state.")
            continue
        best = cands[0]
        loc = ", ".join(p for p in
                        [best["officename"], best["district"], best["state"]] if p)
        warn = "" if best["confident"] else " (best guess — please verify)"
        line = f"{i}. {short}\n   PIN: *{best['pincode']}*{warn}\n   {loc}"
        if len(cands) > 1:
            line += "\n   Also possible: " + ", ".join(c["pincode"] for c in cands[1:])
        parts.append(line)
    return "\n\n".join(parts)
