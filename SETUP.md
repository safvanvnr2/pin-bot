# PIN-code WhatsApp Bot — Setup Guide

The bot code is done. Getting it a phone number that others can message
needs a few things only you can do (Meta requires the number and the
developer account to be yours). It takes about 30–45 minutes.

## Part 1 — Meta / WhatsApp Business setup (you)

1. Go to **developers.facebook.com** and create a developer account
   (log in with Facebook).
2. Create an app → choose **Business** type → add the **WhatsApp** product.
3. In the WhatsApp product page → **API Setup**:
   - You'll get a **test phone number** with a temporary token — good
     enough to try the bot today.
   - Note down the **Phone number ID** and the temporary **access token**.
   - For a permanent bot: add your own business phone number
     (it must be able to receive SMS/voice for verification) and create a
     **permanent system-user token** under App Settings → Advanced or via
     the Business Manager. The test token expires in 24 hours.
4. In App Settings → **Basic**, note the **App secret**.

## Part 2 — Put the bot on a public server (you)

Meta's webhook must reach your bot over a public HTTPS URL. Easiest free
options: **Render.com** or **Railway.app** (both have free tiers).

1. Push this `pin-bot` folder to a GitHub repo.
2. On Render: New → Web Service → connect the repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `python app.py`
   - Also install Tesseract for photo support. On Render add this as the
     build command instead:
     `apt-get update && apt-get install -y tesseract-ocr && pip install -r requirements.txt`
     (needs a `render.yaml` with `env: docker`, or use a Dockerfile.)
3. Add environment variables (Render → Environment):
   - `WHATSAPP_VERIFY_TOKEN` — any random string you invent
   - `WHATSAPP_TOKEN` — token from Part 1
   - `WHATSAPP_PHONE_NUMBER_ID` — phone number ID from Part 1
   - `WHATSAPP_APP_SECRET` — app secret from Part 1
4. Deploy. You'll get a URL like `https://pin-bot-xxxx.onrender.com`.

> The included `data/pincodes.db` (offline backup) is already built and
> ships with the code, so no data step is needed on the server.

## Part 3 — Connect the webhook (you)

1. Back in developers.facebook.com → WhatsApp → **Configuration**.
2. Webhook → Edit → Callback URL: `https://<your-server>/webhook`
   → Verify token: the same `WHATSAPP_VERIFY_TOKEN` string.
3. Verify and save, then under **Webhook fields** subscribe to **messages**.

## Part 4 — Test

Message the bot's number from any WhatsApp account:

```
Connaught Place, New Delhi
MG Road, Bengaluru, Karnataka
```

or send several at once (one per line), or a photo of an address.
The bot replies with a PIN code for each.

## How it works

- Text/photo comes in through the WhatsApp Cloud API webhook.
- Photos are read with Tesseract OCR (free, on-server).
- PIN lookup uses the free `api.postalpincode.in` API (complete,
  up-to-date India Post data, no key needed). If that API is ever down,
  the bot falls back to the bundled offline database.
- Replies go back through the Cloud API. No paid AI service involved.

## Costs

Everything here is free-tier: WhatsApp Cloud API is free for the first
1,000 user conversations/month, Render/Railway have free tiers, and the
PIN data + OCR cost nothing.
