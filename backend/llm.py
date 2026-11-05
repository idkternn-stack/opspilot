"""Lapisan LLM: Gemini dan Groq di balik satu antarmuka, dengan pembatas kecepatan dan fallback.

- complete_json(): satu panggilan yang mengembalikan JSON. Dicoba berurutan: Gemini (model utama,
  model cadangan), lalu Groq (bila GROQ_API_KEY diisi). Kuota habis atau server sibuk langsung
  berpindah ke model/penyedia berikutnya, bukan mencoba ulang berkali-kali.
- search_grounded(): Gemini + Google Search untuk riset perusahaan.
- Pembatas kecepatan per penyedia menjaga permintaan tetap di bawah batas per menit.
"""
import collections
import json
import re
import threading
import time

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from . import config

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class LLMUnavailable(RuntimeError):
    """Semua penyedia LLM sedang tidak bisa dipakai (kuota habis, sibuk, atau belum diatur)."""


class LLMConfigError(RuntimeError):
    """API key ditolak atau model tidak ditemukan. Perlu diperbaiki pengguna."""


class Skip(Exception):
    """Model ini tidak bisa dipakai sekarang; lanjut ke model/penyedia berikutnya."""

    def __init__(self, reason, wait=None):
        super().__init__(reason)
        self.reason, self.wait = reason, wait


class RateLimiter:
    """Jendela geser 60 detik: maksimal `rpm` permintaan per menit."""

    def __init__(self, rpm):
        self.rpm = max(1, rpm)
        self.calls = collections.deque()
        self.lock = threading.Lock()

    def acquire(self, sink=None, name=""):
        while True:
            with self.lock:
                now = time.time()
                while self.calls and now - self.calls[0] >= 60:
                    self.calls.popleft()
                if len(self.calls) < self.rpm:
                    self.calls.append(now)
                    return
                wait = 60 - (now - self.calls[0]) + 0.2
            if sink:
                sink.event("status", {"text": f"Menjaga batas {self.rpm} permintaan/menit ke {name}: menunggu {round(wait)} detik."})
            time.sleep(wait)


_limiters = {}
_cooldown = {}  # (penyedia, model) -> waktu boleh dipakai lagi
_last_reason = {}  # (penyedia, model) -> alasan terakhir dilewati


def _limiter(name, rpm):
    if name not in _limiters or _limiters[name].rpm != max(1, rpm):
        _limiters[name] = RateLimiter(rpm)
    return _limiters[name]


def _extract_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("Jawaban model bukan JSON.")
        return json.loads(m.group(0))


# ---------------------------------------------------------------- Gemini
class GeminiProvider:
    name = "gemini"

    def __init__(self):
        self._client = None

    def configured(self):
        return bool(config.GEMINI_API_KEY)

    def models(self):
        return [m for m in (config.MODEL, config.FALLBACK_MODEL) if m]

    def limiter(self):
        return _limiter(self.name, config.GEMINI_RPM)

    def client(self):
        if self._client is None:
            self._client = genai.Client(api_key=config.GEMINI_API_KEY,
                                        http_options=types.HttpOptions(timeout=config.REQUEST_TIMEOUT_S * 1000))
        return self._client

    @staticmethod
    def _handle(e, model):
        raw = json.dumps(e.details, default=str) if getattr(e, "details", None) else ""
        text = e.message or ""
        if e.code in (400, 401, 403) and ("api key" in text.lower() or "api_key" in raw.lower()):
            key = config.GEMINI_API_KEY
            raise LLMConfigError(f"GEMINI_API_KEY ditolak Google (key berakhiran ...{key[-4:]}). Pesan Google: {text} "
                                 "Buat key baru di AI Studio, isi .env, lalu jalankan ulang start.bat.")
        if e.code == 404:
            raise Skip(f"model {model} tidak tersedia untuk akun ini")
        if e.code == 429:
            daily = "PerDay" in raw
            m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', raw)
            wait = None if daily else (float(m.group(1)) if m else 30.0)
            raise Skip("kuota harian habis" if daily else "batas per menit tercapai", wait)
        if e.code in (500, 503, 504):
            raise Skip(f"server sibuk ({e.code})", 20)
        raise LLMConfigError(f"Gemini API error {e.code}: {text}")

    def complete_json(self, model, system, prompt):
        cfg = types.GenerateContentConfig(system_instruction=system, response_mime_type="application/json")
        try:
            resp = self.client().models.generate_content(model=model, contents=prompt, config=cfg)
        except genai_errors.APIError as e:
            self._handle(e, model)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise Skip(f"koneksi gagal ({e.__class__.__name__})", 10)
        return _extract_json(resp.text)

    def search(self, model, system, prompt):
        cfg = types.GenerateContentConfig(system_instruction=system, tools=[types.Tool(google_search=types.GoogleSearch())])
        try:
            resp = self.client().models.generate_content(model=model, contents=prompt, config=cfg)
        except genai_errors.APIError as e:
            self._handle(e, model)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise Skip(f"koneksi gagal ({e.__class__.__name__})", 10)
        if not resp.candidates or not resp.candidates[0].content:
            raise Skip("tidak ada jawaban")
        cand = resp.candidates[0]
        text = "\n".join(p.text for p in (cand.content.parts or []) if p.text and not p.thought).strip()
        return text, cand.grounding_metadata


# ---------------------------------------------------------------- Groq
class GroqProvider:
    name = "groq"

    def configured(self):
        return bool(config.GROQ_API_KEY)

    def models(self):
        return [m for m in (config.GROQ_MODEL, config.GROQ_FALLBACK_MODEL) if m]

    def limiter(self):
        return _limiter(self.name, config.GROQ_RPM)

    def complete_json(self, model, system, prompt):
        body = {"model": model, "temperature": 0.2, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system + "\nBalas hanya dengan JSON valid."},
                             {"role": "user", "content": prompt}]}
        try:
            r = httpx.post(GROQ_URL, json=body, timeout=config.REQUEST_TIMEOUT_S,
                           headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"})
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise Skip(f"koneksi gagal ({e.__class__.__name__})", 10)
        if r.status_code == 200:
            return _extract_json(r.json()["choices"][0]["message"]["content"])
        text = r.text[:300]
        if r.status_code in (401, 403):
            raise LLMConfigError(f"GROQ_API_KEY ditolak Groq (key berakhiran ...{config.GROQ_API_KEY[-4:]}). "
                                 "Buat key baru di console.groq.com, isi .env, lalu jalankan ulang start.bat.")
        if r.status_code == 404:
            raise Skip(f"model {model} tidak tersedia di Groq")
        if r.status_code == 429:
            daily = "per day" in text.lower() or "(rpd)" in text.lower() or "tokens per day" in text.lower()
            try:
                wait = float(r.headers.get("retry-after", "20"))
            except ValueError:
                wait = 20.0
            raise Skip("kuota harian habis" if daily else "batas per menit tercapai", None if daily else wait)
        if r.status_code == 400 and "json" in text.lower():
            raise Skip("model gagal menghasilkan JSON", 2)
        if r.status_code >= 500:
            raise Skip(f"server sibuk ({r.status_code})", 20)
        raise LLMConfigError(f"Groq API error {r.status_code}: {text}")


PROVIDERS = {"gemini": GeminiProvider(), "groq": GroqProvider()}


def providers():
    return [PROVIDERS[n] for n in config.LLM_ORDER if n in PROVIDERS and PROVIDERS[n].configured()]


def status():
    out = []
    for n in config.LLM_ORDER:
        p = PROVIDERS.get(n)
        if p:
            out.append({"provider": n, "configured": p.configured(), "models": p.models(),
                        "rpm": p.limiter().rpm})
    return out


def _run(candidates, call, sink, purpose):
    """Mencoba (penyedia, model) berurutan. Mengembalikan (hasil, "penyedia/model")."""
    if not candidates:
        raise LLMUnavailable("Belum ada API key LLM. Isi GEMINI_API_KEY (dan opsional GROQ_API_KEY) di .env.")
    reasons, waits = [], []
    for attempt in range(2):  # putaran kedua hanya bila ada model yang cuma perlu menunggu sebentar
        for prov, model in candidates:
            key = (prov.name, model)
            until = _cooldown.get(key, 0)
            if until > time.time():
                if attempt == 0:
                    waits.append(until)
                    mins = max(1, round((until - time.time()) / 60))
                    reasons.append(f"{prov.name}/{model}: {_last_reason.get(key, 'dijeda')} (dicoba lagi ~{mins} menit)")
                    continue
                time.sleep(max(0, min(until - time.time(), 45)))
            prov.limiter().acquire(sink, prov.name)
            try:
                result = call(prov, model)
                _cooldown.pop(key, None)
                return result, f"{prov.name}/{model}"
            except Skip as s:
                reasons.append(f"{prov.name}/{model}: {s.reason}")
                _last_reason[key] = s.reason
                _cooldown[key] = time.time() + (s.wait if s.wait else 3600)
                if sink:
                    sink.event("retry", {"text": f"{prov.name}/{model} {s.reason}. Mencoba penyedia/model berikutnya."})
                if s.wait:
                    waits.append(_cooldown[key])
            except LLMConfigError as e:  # key ditolak: lewati penyedia ini, coba penyedia lain
                reasons.append(f"{prov.name}: {e}")
                for m in prov.models():
                    _cooldown[(prov.name, m)] = time.time() + 300
                    _last_reason[(prov.name, m)] = "API key ditolak"
                if sink:
                    sink.event("retry", {"text": f"{e} Mencoba penyedia lain."})
            except ValueError as e:  # JSON rusak: anggap gagal sementara untuk model ini
                reasons.append(f"{prov.name}/{model}: {e}")
                if sink:
                    sink.event("retry", {"text": f"{prov.name}/{model}: {e} Mencoba berikutnya."})
        soon = [w for w in waits if w - time.time() <= 45]
        if not soon:
            break
    raise LLMUnavailable("Semua model AI sedang tidak bisa dipakai untuk " + purpose + ": " + "; ".join(reasons[-4:]) + ".")


def complete_json(system, prompt, sink=None, purpose="investigasi"):
    cands = [(p, m) for p in providers() for m in p.models()]
    return _run(cands, lambda p, m: p.complete_json(m, system, prompt), sink, purpose)


def search_grounded(system, prompt, sink=None):
    g = PROVIDERS["gemini"]
    if not g.configured():
        raise LLMUnavailable("Riset perusahaan butuh GEMINI_API_KEY (Google Search hanya tersedia di Gemini).")
    return _run([(g, m) for m in g.models()], lambda p, m: p.search(m, system, prompt), sink, "riset Google Search")


def reset_state():
    """Dipakai tes: hapus status cooldown dan pembatas."""
    _cooldown.clear()
    _last_reason.clear()
    _limiters.clear()
