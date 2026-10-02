"""Verification pipeline for the AI Postal PIN Code Verifier.

Every address becomes a record::

    WHATSAPP INPUT -> extract input PIN (never trusted) -> live deterministic
    lookup (India-Post-backed API + offline fallback) -> spelling variations
    (Ponnani->Ponani) -> LIVE WEB SEARCH (Brave API; PIN candidates
    cross-checked: anti-hallucination) -> compare input PIN vs verified PIN
    -> confidence -> WhatsApp report.

Core rule: it is better to report "could not confidently verify" than to
show an incorrect PIN. No PIN is ever invented or guessed.
"""
import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request

from pin_lookup import (find_pincodes, split_addresses, _norm, _api_pincode,
                        _api_search)

PIN_RE = re.compile(r"\b([1-9][0-9]{5})\b")

NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
             "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


# ----------------------------------------------------------------------
# Records
# ----------------------------------------------------------------------
def new_record(original_input, source_kind="text"):
    return {
        "original_input": original_input,
        "normalized_address": "",
        "recipient_name": "",
        "house_or_building": "",
        "street": "",
        "locality": "",
        "village": "",
        "town_city": "",
        "district": "",
        "state": "",
        "country": "India",
        "post_office": "",
        "input_pin": "",
        "verified_pin": "",
        "pin_match": None,
        "verification_status": "unverified",  # verified | mismatch | unverified | empty
        "confidence": "low",                  # high | medium | low
        "sources": [],
        "source_kind": source_kind,           # text | image | document
    }


def extract_input_pin(text):
    """Pull a printed/typed PIN out of the text; return (pin, text_without_pin).

    The PIN is stored as input_pin only — it is never used for lookup.
    """
    text = text or ""
    m = PIN_RE.search(text)
    pin = m.group(1) if m else ""
    query = PIN_RE.sub(" ", text)
    query = re.sub(r"\s+", " ", query).strip(" ,;-")
    return pin, query


# ----------------------------------------------------------------------
# Verification
# ----------------------------------------------------------------------
def _apply_candidate(rec, cand, source, confidence):
    rec["post_office"] = cand.get("officename", "")
    rec["district"] = cand.get("district", "")
    rec["state"] = cand.get("state", "")
    rec["town_city"] = cand.get("district", "")
    rec["verified_pin"] = cand.get("pincode", "")
    rec["confidence"] = confidence
    rec["sources"] = [source]
    rec["verification_status"] = "verified"


def _cross_check_pin(pincode, query, db_path, web_district="", web_state=""):
    """Anti-hallucination: the PIN must really exist and fit the area.

    Checks the PIN against the live API (the offline DB covers only part
    of India, so it is only a backup). The PIN's district/state must match
    the address text or the AI's own district/state answer.
    """
    if not re.fullmatch(r"[1-9][0-9]{5}", pincode or ""):
        return False
    offices = None
    try:
        offices = _api_pincode(pincode)
    except Exception:
        offices = None
    if not offices:
        # Offline DB backup (partial coverage).
        try:
            con = sqlite3.connect(db_path)
            rows = con.execute(
                "SELECT officename, district, state FROM offices "
                "WHERE pincode=? LIMIT 5", (pincode,)).fetchall()
            con.close()
            offices = [{"Name": r[0], "District": r[1], "State": r[2]}
                       for r in rows]
        except Exception:
            return False
    if not offices:
        return False  # PIN exists nowhere -> do not trust it
    qn = _norm(query)
    wd, ws = _norm(web_district), _norm(web_state)
    for o in offices:
        d, s = _norm(o.get("District", "")), _norm(o.get("State", ""))
        if d and re.search(r"\b" + re.escape(d) + r"\b", qn):
            return True
        if s and re.search(r"\b" + re.escape(s) + r"\b", qn):
            return True
        if wd and wd == d:
            return True
        if ws and ws == s:
            return True
    return False


def _tavily_search_texts(query, timeout=20):
    """Web search via Tavily API (free 1k/month, no card; needs TAVILY_API_KEY).

    Returns list of (title + snippet) strings.
    """
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return []
    url = "https://api.tavily.com/search"
    body = json.dumps({
        "api_key": api_key,
        "query": (query or "").strip() + " pincode",
        "max_results": 5,
        "include_answer": False,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"tavily search error: {type(e).__name__}: {e}", flush=True)
        return []
    texts = []
    for res in data.get("results", []):
        texts.append(str(res.get("title", "")) + " " +
                     str(res.get("content", ""))[:500])
    if not texts:
        print(f"tavily: no results for '{query}'", flush=True)
    return texts


def web_search_pins(query, timeout=20):
    """Live web search for PIN candidates (Tavily API; free, no card).

    Returns 6-digit PINs found in search results, in order of appearance.
    """
    pins = []
    # Tavily Search API (reliable, needs TAVILY_API_KEY).
    for text in _tavily_search_texts(query, timeout):
        pins.extend(re.findall(r"\b([1-9][0-9]{5})\b", text))
    seen, out = set(), []
    for p in pins:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out[:10]


def _spelling_variations(token):
    """Generate likely spelling variations for Indian place names.

    India Post often spells names without double letters (Ponnani->Ponani).
    Returns a set of variation strings.
    """
    variations = set()
    # Remove double letters: ponnani -> ponani.
    for i in range(len(token) - 1):
        if token[i] == token[i + 1] and token[i].isalpha():
            variations.add(token[:i] + token[i + 1:])
    return variations


def _verify_via_web_search(query, db_path):
    """Verify a PIN via live web search + anti-hallucination cross-check.

    Returns dict(pincode, post_office, district, state) or None.
    """
    # Get full search texts (not just PINs) for smarter matching.
    texts = _tavily_search_texts(query)
    qn = _norm(query)
    # Pattern 1: Explicit "X Pin code is 123456" statement.
    for text in texts:
        # Find "Pin code is 679333" with place name before it.
        for m in re.finditer(r"(\w[\w\s]{2,40}?)\s+pin\s*code\s+is\s+([1-9][0-9]{5})",
                             text, re.I):
            place, pin = m.group(1).strip(), m.group(2)
            # The place name should resemble our query.
            if _norm(place) and (_norm(place) in qn or qn in _norm(place) or
                any(t in _norm(place) for t in qn.split() if len(t) > 4)):
                try:
                    offices = _api_pincode(pin) or []
                except Exception:
                    offices = []
                if offices:
                    o = offices[0]
                    print(f"web verified (explicit): {pin} ({o.get('Name')})",
                          flush=True)
                    return {
                        "pincode": pin,
                        "post_office": o.get("Name", ""),
                        "district": o.get("District", ""),
                        "state": o.get("State", ""),
                    }
    # Pattern 2: PIN candidates cross-checked against postal data.
    # Use the web text's district mention to validate.
    for text in texts:
        pins = re.findall(r"\b([1-9][0-9]{5})\b", text)
        for pin in dict.fromkeys(pins):  # dedupe, keep order
            try:
                offices = _api_pincode(pin) or []
            except Exception:
                offices = []
            if not offices:
                continue
            o = offices[0]
            district = _norm(o.get("District", ""))
            # The web text should mention the PIN's district.
            if district and re.search(r"\b" + re.escape(district) + r"\b",
                                      _norm(text)):
                print(f"web verified: {pin} ({o.get('Name')})", flush=True)
                return {
                    "pincode": pin,
                    "post_office": o.get("Name", ""),
                    "district": o.get("District", ""),
                    "state": o.get("State", ""),
                }
    return None


def _verify_via_spelling(query, db_path):
    """Fallback: try spelling variations (e.g., Ponnani->Ponani).

    India Post spellings often differ from common usage. This tries
    deterministic variations, prefers an exact office-name match, and
    cross-checks via the live API (anti-hallucination).
    Returns dict(pincode, post_office, district, state) or None.
    """
    q_tokens = [t for t in _norm(query).split() if t]
    for tok in q_tokens:
        if len(tok) < 5:
            continue
        for var in _spelling_variations(tok):
            try:
                offices = _api_search(var) or []
            except Exception:
                continue
            # Prefer the office whose name exactly matches the variation.
            best = None
            for o in offices:
                if _norm(o.get("Name", "")) == var:
                    best = o
                    break
            if best is None and offices:
                best = offices[0]
            if not best:
                continue
            pin = str(best.get("Pincode", ""))
            if pin and _cross_check_pin(pin, query, db_path):
                print(f"spelling verified: {tok}->{var} = {pin} "
                      f"({best.get('Name')})", flush=True)
                return {
                    "pincode": pin,
                    "post_office": best.get("Name", ""),
                    "district": best.get("District", ""),
                    "state": best.get("State", ""),
                }
    return None


def verify_address(raw_text, db_path="data/pincodes.db", llm=None,
                   source_kind="text"):
    """Verify one address; returns a record dict (never raises)."""
    rec = new_record(raw_text, source_kind)
    text = (raw_text or "").strip()
    if not text:
        rec["verification_status"] = "empty"
        return rec

    input_pin, query = extract_input_pin(text)
    rec["input_pin"] = input_pin
    rec["normalized_address"] = query
    if not query:
        # Nothing left but a PIN -> nothing to verify against.
        return rec

    # 1) Deterministic live lookup (India-Post-backed API, offline fallback).
    cands = []
    try:
        cands = find_pincodes(query, db_path) or []
    except Exception as e:
        print(f"lookup error: {type(e).__name__}: {e}", flush=True)
    # First confident candidate wins (a higher-scored vague match must not
    # beat a slightly lower-scored exact one).
    best = next((c for c in cands if c.get("confident")), None)

    if best:
        _apply_candidate(rec, best,
                         source="India Post postal data (live)",
                         confidence="high")
    else:
        # 2) Spelling-variation fallback (deterministic): India Post
        #    spellings often drop double letters (Ponnani->Ponani).
        web = None
        try:
            web = _verify_via_spelling(query, db_path)
        except Exception as e:
            print(f"spelling verify error: {type(e).__name__}: {e}",
                  flush=True)
        if not web:
            # 3) Live WEB SEARCH: find PIN candidates on the web,
            #    cross-check each against real postal data.
            #    This is the mandatory independent verification — it never
            #    trusts a printed PIN and never invents one.
            try:
                web = _verify_via_web_search(query, db_path)
            except Exception as e:
                print(f"web verify error: {type(e).__name__}: {e}",
                      flush=True)
        if web and web.get("pincode"):
            rec["post_office"] = web.get("post_office", "")
            rec["district"] = web.get("district", "")
            rec["state"] = web.get("state", "")
            rec["town_city"] = web.get("district", "")
            rec["verified_pin"] = web["pincode"]
            rec["confidence"] = "medium"
            rec["sources"] = ["live web search, cross-checked"]
            rec["verification_status"] = "verified"
        # else: stays unverified — never invent a PIN.
    # (Gemini AI is used for photo/document vision; the verifiers above are
    # independent and need no API key.)

    # 3) Compare printed/typed PIN against the independently verified one.
    if rec["input_pin"] and rec["verified_pin"]:
        rec["pin_match"] = (rec["input_pin"] == rec["verified_pin"])
        if not rec["pin_match"]:
            rec["verification_status"] = "mismatch"
    return rec


def verify_batch(texts, db_path="data/pincodes.db", llm=None,
                 source_kind="text"):
    """Verify many addresses; one failure never stops the batch."""
    records = []
    for t in texts:
        try:
            records.append(verify_address(t, db_path, llm, source_kind))
        except Exception as e:
            print(f"verify error: {type(e).__name__}: {e}", flush=True)
            rec = new_record(t, source_kind)
            records.append(rec)
    return records


# ----------------------------------------------------------------------
# WhatsApp formatting
# ----------------------------------------------------------------------
def _short(addr, n=60):
    addr = (addr or "").strip()
    return addr if len(addr) <= n else addr[:n - 3] + "..."


def _conf_label(conf):
    return {"high": "High", "medium": "Medium"}.get(conf, "Low")


def _pin_block(rec):
    """Input-PIN vs verified-PIN comparison block."""
    if not rec["input_pin"]:
        return ""
    if rec["verified_pin"]:
        if rec["pin_match"]:
            return f"\n📌 Input PIN: {rec['input_pin']}\n✅ PIN Confirmed"
        return (f"\n📌 Input PIN: {rec['input_pin']}"
                f"\n📮 Verified PIN: *{rec['verified_pin']}*"
                f"\n⚠️ PIN Mismatch — the verified PIN takes precedence")
    return (f"\n📌 Input PIN: {rec['input_pin']}"
            f"\n⚠️ PIN could not be confidently verified")


def format_single(rec):
    kind = rec.get("source_kind", "text")
    header = "📮 PIN CODE VERIFICATION"
    addr_label = ("📷 Extracted address:" if kind == "image"
                  else "📄 Extracted address:" if kind == "document"
                  else "📍 Address:")
    lines = [header, "", addr_label, _short(rec["original_input"], 200)]

    if rec["verification_status"] in ("verified", "mismatch") \
            and rec["verified_pin"]:
        po = rec["post_office"] or "—"
        lines += ["", f"🏤 Post Office: {po}",
                  f"📮 Verified PIN: *{rec['verified_pin']}*"]
        if rec["district"]:
            lines.append(f"📍 District: {rec['district']}")
        if rec["state"]:
            lines.append(f'📍 State: {rec["state"]}')
        if rec["verification_status"] == "verified":
            lines.append("✅ Status: Verified")
        lines.append(f"🔎 Confidence: {_conf_label(rec['confidence'])}")
        if rec["sources"]:
            lines.append(f"🔎 Source: {', '.join(rec['sources'])}")
    else:
        lines += ["",
                  "⚠️ PIN Code could not be confidently verified.",
                  "Please add district, nearby town, Post Office or landmark."]
    pin_block = _pin_block(rec)
    if pin_block:
        lines.append(pin_block)
    return "\n".join(lines).strip()


def _compact_item(rec, idx):
    num = NUM_EMOJI[idx] if idx < len(NUM_EMOJI) else f"{idx + 1}."
    head = f"{num} {_short(rec['original_input'])}"
    if rec["verification_status"] in ("verified", "mismatch") \
            and rec["verified_pin"]:
        po = rec["post_office"] or "—"
        status = "✅ Verified" if rec["verification_status"] == "verified" \
            else "⚠️ PIN Mismatch"
        item = f"{head}\n🏤 {po} | 📮 *{rec['verified_pin']}* | {status}"
        if rec["input_pin"]:
            if rec["pin_match"]:
                item += f"\n   📌 Input PIN {rec['input_pin']} ✅ confirmed"
            else:
                item += (f"\n   📌 Input: {rec['input_pin']} → "
                         f"verified: *{rec['verified_pin']}*")
        return item
    if rec["verification_status"] == "empty":
        return f"{head}\n⚠️ Empty address — skipped."
    return (f"{head}\n⚠️ Could not confidently verify."
            + (f" (printed PIN: {rec['input_pin']})" if rec["input_pin"] else ""))


def format_report(records):
    """Full WhatsApp reply for a batch of verified addresses."""
    records = [r for r in records if r]
    if not records:
        return ("I couldn't find any address in your message. "
                "Send addresses as text, a photo, or a document.")
    if len(records) == 1:
        return format_single(records[0])
    n = len(records)
    parts = [f"📮 PIN CODE VERIFICATION ({n} addresses)"]
    parts += [_compact_item(r, i) for i, r in enumerate(records)]
    return "\n\n".join(parts).strip()


def split_message(text, limit=4000):
    """Split a long reply on blank lines so no WhatsApp message is cut."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur = (cur + "\n\n" + para) if cur else para
    if cur:
        chunks.append(cur)
    return chunks
