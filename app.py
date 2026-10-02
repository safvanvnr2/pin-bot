"""WHATSAPP AI POSTAL PIN CODE VERIFIER.

Receives WhatsApp Cloud API webhooks (text / image / document / audio),
independently verifies the PIN code for every address via live postal data
and AI web search, and replies with a verification report.

Core rule: NEVER trust a PIN printed by the user or on an image.
Every PIN is verified independently; mismatches are reported.

Setup: copy .env.example to .env and fill the values (see SETUP.md).
Optional: GEMINI_API_KEY enables AI vision + web verification.
"""
import hashlib
import hmac
import os
import sys
import tempfile
import threading

import requests
from flask import Flask, request, abort

from llm import GeminiClient
from pin_lookup import split_addresses, split_ocr_text, ocr_image
from verifier import verify_batch, format_report, split_message

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "")
DB_PATH = os.environ.get("PINCODE_DB", os.path.join("data", "pincodes.db"))
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
SERPER_KEY = os.environ.get("SERPER_API_KEY", "").strip()
if SERPER_KEY:
    print("Serper web search: configured.", flush=True)
else:
    print("NOTE: SERPER_API_KEY not set — web search disabled.", flush=True)

GRAPH = "https://graph.facebook.com/v21.0"

WELCOME = (
    "Hi! I'm the AI Postal PIN Code Verifier. Send me an Indian address — "
    "as text, a photo, or a document — and I'll independently verify its "
    "PIN code. You can send several addresses at once."
)


def get_llm():
    if not GEMINI_KEY:
        return None
    return GeminiClient(GEMINI_KEY, GEMINI_MODEL)


def verify_signature(payload: bytes) -> bool:
    """Validate X-Hub-Signature-256 using the app secret (if configured)."""
    if not APP_SECRET:
        return True
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not sig.startswith("sha256="):
        return False
    expected = hmac.new(APP_SECRET.encode(), payload,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig[7:], expected)


def send_text(to: str, body: str):
    """Send a WhatsApp text message via the Cloud API (splits long texts)."""
    for chunk in split_message(body):
        resp = requests.post(
            f"{GRAPH}/{PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}",
                     "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": to,
                  "type": "text", "text": {"body": chunk}},
            timeout=30,
        )
        resp.raise_for_status()


def download_media_bytes(media_id: str):
    """Download incoming media; return (bytes, mime_type)."""
    meta = requests.get(
        f"{GRAPH}/{media_id}",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
        timeout=30).json()
    url = meta.get("url")
    if not url:
        raise RuntimeError("Could not get media URL from WhatsApp.")
    r = requests.get(url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                     timeout=90)
    r.raise_for_status()
    return r.content, meta.get("mime_type", "")


# ----------------------------------------------------------------------
# Handlers (run in background threads; never leave the user hanging)
# ----------------------------------------------------------------------
def handle_text(sender: str, text: str):
    llm = get_llm()
    addrs = split_addresses(text)
    if len(addrs) > 2:
        send_text(sender, f"🔍 Found {len(addrs)} addresses. Verifying PIN codes…")
    records = verify_batch(addrs, DB_PATH, llm, source_kind="text")
    send_text(sender, format_report(records))


def _vision_or_tesseract(data: bytes, mime: str, llm):
    """Extract address texts from image bytes.

    Returns a list of {"text", "input_pin"}. Prefers AI vision; falls back
    to Tesseract OCR when no API key is configured.
    """
    if llm is not None and llm.available:
        try:
            out = llm.extract_addresses_from_image(data, mime)
        except Exception as e:
            print(f"vision error: {type(e).__name__}: {e}", flush=True)
            out = None
        if out:
            return out
        # AI found nothing -> fall through to Tesseract before giving up.
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".jpg", prefix="pinbot-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        text = ocr_image(path)
        # Group lines into address blocks; don't split one address per line.
        return [{"text": t, "input_pin": ""} for t in split_ocr_text(text)]
    except RuntimeError as e:
        raise RuntimeError(str(e))
    finally:
        if path and os.path.exists(path):
            os.remove(path)


def handle_image(sender: str, media_id: str, mime: str):
    llm = get_llm()
    try:
        data, mime = download_media_bytes(media_id)
    except Exception as e:
        send_text(sender, f"Couldn't download that photo: {e}")
        return
    try:
        extracted = _vision_or_tesseract(data, mime, llm)
    except RuntimeError as e:
        send_text(sender, f"Couldn't read that photo: {e}")
        return
    if not extracted:
        send_text(sender, "I couldn't find any address in that photo. "
                          "Try a clearer, well-lit picture.")
        return
    texts = []
    for a in extracted:
        t = a["text"]
        if a.get("input_pin") and a["input_pin"] not in t:
            t = f"{t}\nPIN printed on image: {a['input_pin']}"
        texts.append(t)
    if len(texts) > 2:
        send_text(sender, f"🔍 Found {len(texts)} addresses in the photo. Verifying…")
    records = verify_batch(texts, DB_PATH, llm, source_kind="image")
    send_text(sender, format_report(records))


def _pdf_text_fallback(data: bytes):
    """Best-effort text extraction from a PDF without AI."""
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        return ""


def handle_document(sender: str, media_id: str, mime: str, filename: str):
    llm = get_llm()
    try:
        data, mime = download_media_bytes(media_id)
    except Exception as e:
        send_text(sender, f"Couldn't download that document: {e}")
        return

    extracted = None
    if llm is not None and llm.available:
        try:
            extracted = llm.extract_addresses_from_document(
                data, mime or "application/pdf", filename or "")
        except Exception as e:
            print(f"doc vision error: {type(e).__name__}: {e}", flush=True)

    texts = []
    if extracted:
        for a in extracted:
            t = a["text"]
            if a.get("input_pin") and a["input_pin"] not in t:
                t = f"{t}\nPIN printed on document: {a['input_pin']}"
            texts.append(t)
    elif (mime or "").lower() == "application/pdf" or \
            (filename or "").lower().endswith(".pdf"):
        text = _pdf_text_fallback(data)
        texts = split_ocr_text(text)
    else:
        send_text(sender, "I can read PDF documents and photos. For other "
                          "files, please send the address as text or a photo.")
        return

    if not texts:
        send_text(sender, "I couldn't find any address in that document.")
        return
    if len(texts) > 2:
        send_text(sender, f"🔍 Found {len(texts)} addresses in the document. Verifying…")
    records = verify_batch(texts, DB_PATH, llm, source_kind="document")
    send_text(sender, format_report(records))


def handle_message(sender: str, message: dict):
    """Route one WhatsApp message; always answers, never raises."""
    mtype = message.get("type")
    try:
        if mtype == "text":
            handle_text(sender, message["text"]["body"])
        elif mtype == "image":
            img = message["image"]
            handle_image(sender, img["id"], img.get("mime_type", ""))
        elif mtype == "document":
            doc = message["document"]
            handle_document(sender, doc["id"], doc.get("mime_type", ""),
                            doc.get("filename", ""))
        elif mtype == "audio":
            send_text(sender, "🔇 I can't process voice messages. Please send "
                              "the address as text, a photo, or a document.")
        else:
            send_text(sender, "I read text, photo, and document addresses. " + WELCOME)
    except Exception as e:
        print(f"handler error: {type(e).__name__}: {e}", flush=True)
        try:
            send_text(sender, "Something went wrong looking that up. "
                              "Please try again.")
        except Exception as e2:
            print(f"reply error: {type(e2).__name__}: {e2}", flush=True)


# ----------------------------------------------------------------------
# Webhook
# ----------------------------------------------------------------------
@app.get("/webhook")
def webhook_verify():
    """Meta's webhook verification handshake."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "verification failed", 403


@app.post("/webhook")
def webhook_receive():
    if not verify_signature(request.get_data()):
        abort(403)
    payload = request.get_json(force=True, silent=True) or {}
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                sender = message.get("from")
                mtype = message.get("type")
                print(f"msg from={sender} type={mtype}", flush=True)
                if not sender:
                    continue
                # Process in the background so slow AI/web verification
                # never blocks the webhook (or hits server timeouts).
                threading.Thread(target=handle_message,
                                 args=(sender, message),
                                 daemon=True).start()
    return "ok", 200


@app.get("/")
def index():
    return "AI Postal PIN Code Verifier is running. Webhook: /webhook", 200


if __name__ == "__main__":
    missing = [k for k, v in {
        "WHATSAPP_VERIFY_TOKEN": VERIFY_TOKEN,
        "WHATSAPP_TOKEN": WHATSAPP_TOKEN,
        "WHATSAPP_PHONE_NUMBER_ID": PHONE_NUMBER_ID,
    }.items() if not v]
    if missing:
        print("WARNING: missing env vars:", ", ".join(missing))
        print("Copy .env.example to .env and fill them in (see SETUP.md).")
    if not GEMINI_KEY:
        print("NOTE: GEMINI_API_KEY not set — AI vision/web verification "
              "disabled; using built-in lookup only.")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
