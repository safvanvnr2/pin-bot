# WHATSAPP AI POSTAL PIN CODE VERIFIER — Setup Guide

## What it does

- You send an Indian address as **text, a photo, or a document (PDF)**.
- The bot **independently verifies** the PIN code — it never trusts a PIN
  you typed or one printed on a photo. If they differ, it reports
  **⚠️ PIN Mismatch**.
- Multiple addresses in one message/photo/document are each verified
  separately and returned as a numbered report.
- Every result has a **confidence** (High/Medium/Low) and a **source**.
  The bot never guesses — if it can't verify, it says so.
- No voice messages (by design).

## Part 1 — Meta / WhatsApp Business setup (you)

1. Go to **developers.facebook.com**, create a developer account, create an
   app (Business type), add the **WhatsApp** product.
2. In WhatsApp → **API Setup**: note the **Phone number ID** and the
   temporary **access token** (good for testing today).
3. For a permanent bot: add your own business number and create a
   **permanent system-user token** (the test token expires in ~24 hours).
4. In App Settings → **Basic**, note the **App secret**.

## Part 2 — Free AI key for photo reading + web verification (you, 2 min)

The bot reads photos/documents and double-checks tricky addresses with
Google's Gemini AI (free tier).

1. Go to **https://aistudio.google.com/apikey** → **Create API Key**.
2. Copy the key. You'll paste it into Render in Part 3.
3. Without the key the bot still works (built-in OCR + postal lookup),
   but AI photo reading and AI web verification are disabled.

## Part 3 — Put the bot on a public server (you)

1. Push this `pin-bot` folder to a GitHub repo.
2. On **Render.com**: New → Web Service → connect the repo.
   (The included `Dockerfile` handles Tesseract OCR and the server;
   Render rebuilds automatically on every push.)
3. Add environment variables (Render → Environment):
   - `WHATSAPP_VERIFY_TOKEN` — any random string you invent
   - `WHATSAPP_TOKEN` — token from Part 1
   - `WHATSAPP_PHONE_NUMBER_ID` — phone number ID from Part 1
   - `WHATSAPP_APP_SECRET` — app secret from Part 1
   - `GEMINI_API_KEY` — key from Part 2 (optional but recommended)
4. Deploy. You'll get a URL like `https://pin-bot-xxxx.onrender.com`.

## Part 4 — Connect the webhook (you)

1. developers.facebook.com → WhatsApp → **Configuration**.
2. Webhook → Edit → Callback URL: `https://<your-server>/webhook`
   → Verify token: your `WHATSAPP_VERIFY_TOKEN`.
3. Verify and save, then under **Webhook fields** subscribe to **messages**.

## Part 5 — Test

Message the bot's number:

```
Tirur Railway Station, Malappuram, Kerala
Kottakkal, Malappuram, Kerala 676503
```

Send several addresses at once (one per line), a photo of a parcel label,
or a PDF. The bot replies with a verification report for each.

## How it works

- **Input**: text / image / document come in via the WhatsApp Cloud API.
  Images and PDFs are read by Gemini AI vision (falls back to on-server
  Tesseract OCR if no AI key). Voice messages get a polite refusal.
- **Verify** (per address):
  1. Any typed/printed PIN is stored as `input_pin` only — never trusted.
  2. Live lookup via the free `api.postalpincode.in` API (India Post
     data), with district-aware scoring and an offline DB fallback.
  3. If uncertain and the AI key is set, Gemini performs a live web
     search (prioritising India Post / official sources) and the result is
     cross-checked against real postal data (anti-hallucination).
  4. `input_pin` vs verified PIN → ✅ Confirmed / ⚠️ Mismatch.
  5. Confidence High/Medium/Low; never presented as verified when Low.
- **Reply**: single detailed report, or a compact numbered list for
  batches. Long replies are split automatically.

## Costs

Free-tier throughout: WhatsApp Cloud API (1,000 conversations/month
free), Render free tier, Gemini AI free tier, postal API + OCR free.
