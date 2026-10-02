"""Test runner: reads WhatsApp secrets from stdin (never stored in any file,
env var, or log), injects them into the app module, and starts the server.

Stdin lines, in order:
  1. WHATSAPP_TOKEN (temporary access token)
  2. WHATSAPP_PHONE_NUMBER_ID
  3. WHATSAPP_VERIFY_TOKEN
"""
import sys


def main():
    token = sys.stdin.readline().strip()
    phone_id = sys.stdin.readline().strip()
    verify = sys.stdin.readline().strip()
    if not (token and phone_id and verify):
        sys.exit("missing secrets on stdin")
    import app as bot
    bot.WHATSAPP_TOKEN = token
    bot.PHONE_NUMBER_ID = phone_id
    bot.VERIFY_TOKEN = verify
    print("bot server starting on 127.0.0.1:5000", flush=True)
    bot.app.run(host="127.0.0.1", port=5000, threaded=True)


if __name__ == "__main__":
    main()
