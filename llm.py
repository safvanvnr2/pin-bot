"""Google Gemini client for AI vision extraction and web-grounded verification.

Free API key: https://aistudio.google.com/apikey

Every method fails soft (returns None) so the bot degrades gracefully when
the key is missing, the quota is exhausted, or the API misbehaves. The
deterministic postal lookup in pin_lookup.py keeps working without AI.
"""
import base64
import json
import re

import requests

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.0-flash"

PIN_RE = re.compile(r"\b([1-9][0-9]{5})\b")


def _parse_json(text):
    """Extract a JSON object/array from model output, defensively."""
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # Strip code fences like ```json ... ```
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None
    return None


class GeminiClient:
    def __init__(self, api_key, model=None):
        self.api_key = (api_key or "").strip()
        self.model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL

    @property
    def available(self):
        return bool(self.api_key)

    def _discover_model(self):
        """Find a working generateContent model (model names change over time)."""
        try:
            r = requests.get(f"{API_BASE}/models",
                             params={"key": self.api_key},
                             timeout=30)
            r.raise_for_status()
            models = r.json().get("models", [])
        except Exception as e:
            print(f"Gemini model discovery failed: {type(e).__name__}: {e}",
                  flush=True)
            return None
        cands = []
        for m in models:
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            name = str(m.get("name", "")).split("/")[-1]
            if not name:
                continue
            # Prefer flash (fast, free-tier friendly), then pro, then others.
            rank = (0 if "flash" in name else 1 if "pro" in name else 2)
            cands.append((rank, name))
        cands.sort()
        if cands:
            print(f"Gemini: discovered model {cands[0][1]}", flush=True)
            return cands[0][1]
        return None

    def _try_generate(self, parts, use_search, timeout, json_mode=True):
        """One API attempt. Returns parsed JSON/text, None on error,
        or 'MODEL_NOT_FOUND' on 404."""
        url = f"{API_BASE}/models/{self.model}:generateContent"
        body = {"contents": [{"parts": parts}]}
        if json_mode:
            body["generationConfig"] = {
                "temperature": 0.1,
                "response_mime_type": "application/json",
            }
        else:
            body["generationConfig"] = {"temperature": 0.2}
        if use_search:
            body["tools"] = [{"google_search": {}}]
        try:
            r = requests.post(url, params={"key": self.api_key},
                              json=body, timeout=timeout)
            if r.status_code == 404:
                return "MODEL_NOT_FOUND"
            if r.status_code == 429:
                print("Gemini: rate limited (429); degrading to "
                      "deterministic lookup.", flush=True)
                return None
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"Gemini API error: {type(e).__name__}: {e}", flush=True)
            return None
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            print("Gemini: unexpected response shape.", flush=True)
            return None
        if not json_mode:
            return text
        parsed = _parse_json(text)
        if parsed is None:
            print(f"Gemini: non-JSON response (first 200 chars): {text[:200]}",
                  flush=True)
        return parsed

    def _generate(self, parts, use_search=False, timeout=120, json_mode=True):
        if not self.api_key:
            return None
        result = self._try_generate(parts, use_search, timeout, json_mode)
        if result != "MODEL_NOT_FOUND":
            return result
        # Model name outdated -> discover a working one and retry once.
        print(f"Gemini: model {self.model} not found; discovering...",
              flush=True)
        new_model = self._discover_model()
        if not new_model or new_model == self.model:
            return None
        self.model = new_model
        return self._try_generate(parts, use_search, timeout, json_mode)

    # ------------------------------------------------------------------
    # Vision: extract addresses from images / documents
    # ------------------------------------------------------------------
    _EXTRACT_PROMPT = (
        "You are a postal address reader. Extract every postal address visible "
        "in this image. Read printed AND handwritten text, including Indian "
        "language scripts (Malayalam, Hindi, Tamil, Kannada, Telugu, Bengali, "
        "Marathi, Gujarati, Punjabi, Urdu); transliterate to English where "
        "needed but keep the original meaning. The image may be blurry, "
        "rotated, or low quality — do your best. Fix only obvious OCR-style "
        "character errors using geographic context (e.g. O/0, I/1/l); never "
        "invent address parts, and never silently change a location name if "
        "the change could alter the actual place.\n"
        "Return JSON: {\"addresses\": [{\"text\": \"full address as a single "
        "string\", \"input_pin\": \"6-digit PIN printed in the image, or null "
        "if none\"}]}. If no address is visible, return {\"addresses\": []}."
    )

    def _extract(self, data: bytes, mime: str, timeout=150):
        if not data:
            return None
        b64 = base64.b64encode(data).decode("ascii")
        parts = [
            {"text": self._EXTRACT_PROMPT},
            {"inline_data": {"mime_type": mime, "data": b64}},
        ]
        out = self._generate(parts, timeout=timeout)
        if not isinstance(out, dict):
            return None
        addrs = out.get("addresses")
        if not isinstance(addrs, list):
            return None
        cleaned = []
        for a in addrs:
            if not isinstance(a, dict):
                continue
            text = str(a.get("text") or "").strip()
            if not text:
                continue
            pin = str(a.get("input_pin") or "").strip()
            if pin and not PIN_RE.fullmatch(pin):
                pin = ""
            cleaned.append({"text": text, "input_pin": pin})
        return cleaned

    def extract_addresses_from_image(self, data: bytes, mime: str):
        """Vision OCR for photos, labels, screenshots, signboards."""
        return self._extract(data, mime or "image/jpeg")

    def extract_addresses_from_document(self, data: bytes, mime: str,
                                        filename: str = ""):
        """Vision extraction for documents (PDF pages etc.)."""
        return self._extract(data, mime or "application/pdf")

    # ------------------------------------------------------------------
    # Web-grounded PIN verification
    # ------------------------------------------------------------------
    _VERIFY_PROMPT = (
        "You are an AI postal verification specialist for Indian addresses. "
        "Use web search RIGHT NOW to verify the correct India Post PIN code "
        "for the address below. Prioritize indiapost.gov.in and official "
        "Indian government postal sources; cross-check with at least one "
        "other reputable postal directory or map source when possible.\n"
        "Address: {address}\n"
        "Identify the exact locality, village/town/city, district, state, "
        "and the post office that serves it. The PIN must be the one for "
        "that post office's delivery area — not just the city's main PIN.\n"
        "Return JSON: {{\"post_office\": \"name or null\", "
        "\"pincode\": \"6 digits or null\", \"district\": \"name or null\", "
        "\"state\": \"name or null\", \"confidence\": \"high|medium|low\", "
        "\"sources\": [\"short source names\"]}}.\n"
        "ANTI-HALLUCINATION RULES: Never invent a PIN. Only report a pincode "
        "you actually found in search results from a postal source. Never "
        "report a nearby PIN because it looks plausible. If you cannot verify "
        "from reliable sources, set pincode to null and confidence to low."
    )

    def verify_with_search(self, address: str):
        """Independently verify a PIN via live web search (falls back to
        model knowledge if search grounding is unavailable).

        Returns dict(post_office, pincode, district, state, confidence,
        sources) or None when verification is impossible / API fails.
        """
        address = (address or "").strip()
        if not address:
            return None
        prompt = self._VERIFY_PROMPT.format(address=address)
        # Try with live web search first.
        out = self._generate([{"text": prompt}], use_search=True, timeout=150)
        print(f"Gemini verify result: {str(out)[:250]}", flush=True)
        if out == "MODEL_NOT_FOUND" or out is None:
            # Search grounding may be unsupported; try model knowledge.
            # The cross-check below still guards against hallucination.
            print("Gemini: retrying verification without web search...",
                  flush=True)
            out = self._generate([{"text": prompt}], use_search=False,
                                 timeout=150)
            print(f"Gemini verify (no search) result: {str(out)[:250]}",
                  flush=True)
        if not isinstance(out, dict):
            return None
        pin = str(out.get("pincode") or "").strip()
        if pin and not PIN_RE.fullmatch(pin):
            return None  # malformed -> treat as unverified
        conf = str(out.get("confidence") or "low").lower()
        if conf not in ("high", "medium", "low"):
            conf = "low"
        return {
            "post_office": str(out.get("post_office") or "").strip(),
            "pincode": pin,
            "district": str(out.get("district") or "").strip(),
            "state": str(out.get("state") or "").strip(),
            "confidence": conf,
            "sources": [str(s) for s in (out.get("sources") or [])][:3],
        }
