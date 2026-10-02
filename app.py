"""WhatsApp PIN-code bot server.

Receives WhatsApp Cloud API webhooks, finds the PIN code for every address
in the incoming message (text or photo), and replies with the results.

Setup: copy .env.example to .env and fill in the values (see SETUP.md),
then run:  python app.py
"""
import hashlib
import hmac
import os
import tempfile

import requests
from flask import Flask, request, abort

from pin_lookup import lookup_message, format_results, ocr_image

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "")
DB_PATH = os.environ.get("PINCODE_DB", os.path.join("data", "pincodes.db"))

GRAPH = "https://graph.facebook.com/v21.0"

WELCOME = (
    "Hi! Send me any Indian address (or several, one per line), as text "
    "or a photo, and I'll reply with the PIN code for each."
)


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
    """Send a WhatsApp text message via the Cloud API."""
    resp = requests.post(
        f"{GRAPH}/{PHONE_NUMBER_ID}/messages",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}",
                 "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to,
              "type": "text", "text": {"body": body}},
        timeout=30,
    )
    resp.raise_for_status()


def download_media(media_id: str) -> str:
    """Download an incoming image; return the local temp file path."""
    meta = requests.get(
        f"{GRAPH}/{media_id}",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
        timeout=30).json()
    url = meta.get("url")
    if not url:
        raise RuntimeError("Could not get media URL from WhatsApp.")
    r = requests.get(url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                     timeout=60)
    r.raise_for_status()
    suffix = ".jpg"
    mime = meta.get("mime_type", "")
    if "png" in mime:
        suffix = ".png"
    elif "webp" in mime:
        suffix = ".webp"
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="pinbot-")
    with os.fdopen(fd, "wb") as f:
        f.write(r.content)
    return path


def handle_text_message(sender: str, text: str):
    results = lookup_message(text, DB_PATH)
    send_text(sender, format_results(results))


def handle_image_message(sender: str, media_id: str):
    path = None
    try:
        path = download_media(media_id)
        address_text = ocr_image(path)
        results = lookup_message(address_text, DB_PATH)
        reply = format_results(results)
    except RuntimeError as e:
        reply = f"Couldn't read that photo: {e}"
    finally:
        if path and os.path.exists(path):
            os.remove(path)
    send_text(sender, reply)


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
    import sys as _sys
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                sender = message.get("from")
                mtype = message.get("type")
                print(f"DEBUG msg from={sender} type={mtype}", file=_sys.stderr, flush=True)
                if not sender:
                    continue
                try:
                    if mtype == "text":
                        handle_text_message(sender, message["text"]["body"])
                    elif mtype == "image":
                        handle_image_message(sender, message["image"]["id"])
                    else:
                        send_text(sender,
                                  "I can read text and photo addresses only. " + WELCOME)
                except Exception as e:  # never leave the user hanging
                    print(f"DEBUG handler error: {type(e).__name__}: {e}", file=_sys.stderr, flush=True)
                    try:
                        send_text(sender,
                                  f"Something went wrong looking that up ({e}). "
                                  "Please try again.")
                    except Exception as e2:
                        print(f"DEBUG reply error: {type(e2).__name__}: {e2}", file=_sys.stderr, flush=True)
    return "ok", 200


@app.get("/")
def index():
    return "PIN-code bot is running. Webhook: /webhook", 200


if __name__ == "__main__":
    missing = [k for k, v in {
        "WHATSAPP_VERIFY_TOKEN": VERIFY_TOKEN,
        "WHATSAPP_TOKEN": WHATSAPP_TOKEN,
        "WHATSAPP_PHONE_NUMBER_ID": PHONE_NUMBER_ID,
    }.items() if not v]
    if missing:
        print("WARNING: missing env vars:", ", ".join(missing))
        print("Copy .env.example to .env and fill them in (see SETUP.md).")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
