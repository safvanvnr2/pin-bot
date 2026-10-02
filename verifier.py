"""Verification pipeline for the AI Postal PIN Code Verifier.

Every address becomes a record::

    WHATSAPP INPUT -> extract input PIN (never trusted) -> live deterministic
    lookup (India-Post-backed API + offline fallback) -> optional AI web
    verification (cross-checked: anti-hallucination) -> compare input PIN vs
    verified PIN -> confidence -> WhatsApp report.

Core rule: it is better to report "could not confidently verify" than to
show an incorrect PIN. No PIN is ever invented or guessed.
"""
import re
import sqlite3

from pin_lookup import find_pincodes, split_addresses, _norm, _api_pincode

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
    elif llm is not None and llm.available:
        # 2) AI web verification, cross-checked against our postal DB.
        web = None
        try:
            web = llm.verify_with_search(query)
        except Exception as e:
            print(f"AI verify error: {type(e).__name__}: {e}", flush=True)
        if web and web.get("pincode") and _cross_check_pin(
                web["pincode"], query, db_path,
                web.get("district", ""), web.get("state", "")):
            rec["post_office"] = web.get("post_office", "")
            rec["district"] = web.get("district", "")
            rec["state"] = web.get("state", "")
            rec["town_city"] = web.get("district", "")
            rec["verified_pin"] = web["pincode"]
            rec["confidence"] = ("high" if web.get("confidence") == "high"
                                 else "medium")
            rec["sources"] = web.get("sources") or ["web verification"]
            rec["verification_status"] = "verified"
        # else: stays unverified — never invent a PIN.
    # else: stays unverified.

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
            lines.append(f"📍 State: {rec["state"]}")
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
