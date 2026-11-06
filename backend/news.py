"""Sumber berita cadangan: Google News RSS (gratis, tanpa API key).

Dipakai riset perusahaan bila Google Search di Gemini kena batas kuota. Setiap artikel diberi ID
(A1, A2, ...) dan AI hanya boleh membuat temuan yang merujuk ID artikel itu; kode membuang temuan
yang merujuk artikel yang tidak ada.
"""
import datetime as dt
import email.utils
import re
import urllib.parse
import xml.etree.ElementTree as ET

import httpx

FEEDS = [("id", "ID", "ID:id"), ("en-US", "US", "US:en")]
MAX_ARTICLES = 25
OPERATIONAL_HINTS = ("stok", "langka", "kosong", "gangguan", "error", "down", "tutup", "rugi", "recall", "tarik", "harga",
                     "diskon", "denda", "gugat", "sanksi", "pengiriman", "terlambat", "PHK", "mogok", "siber", "hack",
                     "bocor", "shortage", "outage", "closure", "lawsuit", "fine", "delay", "strike", "layoff", "breach",
                     "cyber", "price", "supply", "losses", "bankrupt")


class NewsError(RuntimeError):
    pass


def _parse(xml_text, max_age_days):
    root = ET.fromstring(xml_text)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        src = item.find("source")
        source = (src.text or "").strip() if src is not None else ""
        source_url = src.get("url", "") if src is not None else ""
        try:
            published = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            published = None
        if not title or not link.startswith("https://"):
            continue
        if published and published.tzinfo and published < cutoff:
            continue
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        out.append({"title": title, "url": link, "source": source,
                    "domain": urllib.parse.urlparse(source_url).netloc.lower() or source.lower(),
                    "date": published.date().isoformat() if published else ""})
    return out


STOPWORDS = set("""yang dan di ke dari untuk dengan pada ini itu atau tapi tetapi namun karena akan sudah telah masih
juga oleh dalam sebagai bisa tidak ada lebih para saat setelah serta saya kami kita mereka the a an of to in on for and or
but with by from is are was were be has have had at as its it this that pt tbk juta ribu rupiah miliar triliun
tetap dinyatakan menjadi baru hari usd rp""".split())


def keywords(text, limit=6):
    """Kata kunci berita dari judul kasus: kata bermakna, tanpa kata umum dan angka."""
    out = []
    for w in re.findall(r"[A-Za-zÀ-ÿ]{3,}", text or ""):
        lw = w.lower()
        if lw not in STOPWORDS and lw not in out:
            out.append(lw)
    return out[:limit]


def search(query, max_age_days=180, limit=8, timeout=15, client=None):
    """Tool agent: mencari artikel Google News untuk satu kueri (Indonesia dan global)."""
    query = re.sub(r"[\r\n\t<>{}`]", " ", str(query or "")).strip()[:150]
    if not query:
        raise NewsError("Kueri berita kosong.")
    get = (client or httpx).get
    articles, seen, errors = [], set(), []
    for hl, gl, ceid in FEEDS:
        url = (f"https://news.google.com/rss/search?q={urllib.parse.quote(query + f' when:{max_age_days}d')}"
               f"&hl={hl}&gl={gl}&ceid={ceid}")
        try:
            r = get(url, timeout=timeout, headers={"User-Agent": "OpsPilot/1.0 (riset berita publik)"})
            r.raise_for_status()
            for a in _parse(r.text, max_age_days):
                key = re.sub(r"\W+", " ", a["title"].lower()).strip()
                if key not in seen:
                    seen.add(key)
                    articles.append(a)
        except (httpx.HTTPError, ET.ParseError) as e:
            errors.append(f"{hl}: {e.__class__.__name__}")
    if not articles and errors:
        raise NewsError("Google News tidak bisa diakses (" + "; ".join(errors) + ").")
    articles.sort(key=lambda a: a["date"] or "", reverse=True)
    return [{"judul": a["title"], "media": a["source"], "tanggal": a["date"], "url": a["url"]} for a in articles[:limit]]


def fetch(company, max_age_days=90, timeout=15, client=None):
    """Mengambil artikel terbaru tentang perusahaan dari Google News (Indonesia dan global)."""
    get = (client or httpx).get
    articles, seen, errors = [], set(), []
    for hl, gl, ceid in FEEDS:
        q = urllib.parse.quote(f'"{company}" when:{max_age_days}d')
        url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
        try:
            r = get(url, timeout=timeout, headers={"User-Agent": "OpsPilot/1.0 (riset berita publik)"})
            r.raise_for_status()
            for a in _parse(r.text, max_age_days):
                key = re.sub(r"\W+", " ", a["title"].lower()).strip()
                if key not in seen:
                    seen.add(key)
                    articles.append(a)
        except (httpx.HTTPError, ET.ParseError) as e:
            errors.append(f"{hl}: {e.__class__.__name__}")
    if not articles and errors:
        raise NewsError("Google News tidak bisa diakses (" + "; ".join(errors) + ").")
    # utamakan judul yang menyinggung masalah operasional, lalu yang terbaru
    hint = lambda a: any(h.lower() in a["title"].lower() for h in OPERATIONAL_HINTS)
    articles.sort(key=lambda a: a["date"] or "", reverse=True)
    articles.sort(key=lambda a: not hint(a))
    for i, a in enumerate(articles[:MAX_ARTICLES], 1):
        a["id"] = f"A{i}"
    return articles[:MAX_ARTICLES]
