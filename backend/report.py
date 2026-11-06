"""Laporan analisis siap cetak (dibuka di browser, lalu Cetak -> Simpan sebagai PDF)."""
import datetime as dt
import html
import re

STATUS = {"approved": "Disetujui dan dijalankan", "rejected": "Ditolak", "failed": "Gagal dijalankan",
          "pending": "Menunggu persetujuan"}

CSS = """
*{box-sizing:border-box}body{font:14px/1.55 Georgia,'Times New Roman',serif;color:#14181f;background:#fff;margin:0}
main{max-width:820px;margin:0 auto;padding:32px 28px 48px}
h1{font:600 22px/1.3 system-ui,sans-serif;margin:0 0 4px}h2{font:600 16px/1.3 system-ui,sans-serif;margin:26px 0 8px;
border-bottom:1px solid #dde1e7;padding-bottom:4px}h3{font:600 14px system-ui,sans-serif;margin:14px 0 4px}
.meta{font:13px system-ui,sans-serif;color:#5d6673}.box{border:1px solid #dde1e7;border-radius:6px;padding:10px 14px;margin:8px 0}
.note{background:#f6f7f9}ul,ol{margin:4px 0 4px 20px;padding:0}a{color:#0b6e4f}
table{border-collapse:collapse;width:100%;font:13px system-ui,sans-serif}td,th{border-bottom:1px solid #eef0f3;padding:5px 6px;
text-align:left;vertical-align:top}th{color:#5d6673;font-weight:500}
.bar{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:18px;font:13px system-ui,sans-serif}
button{font:inherit;border:1px solid #0b6e4f;background:#0b6e4f;color:#fff;border-radius:6px;padding:6px 14px;cursor:pointer}
@media print{.bar{display:none}main{padding:0}a{color:#14181f}@page{size:A4;margin:18mm}}
"""


def _e(x):
    return html.escape(str(x if x is not None else ""))


def _md(text):
    """Markdown sederhana dari ringkasan agent (tebal dan daftar)."""
    out, in_list = [], False
    for raw in (text or "").split("\n"):
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", _e(raw))
        m = re.match(r"^\s*[-*•]\s+(.*)", line)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{m.group(1)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.strip():
            out.append(f"<p>{line}</p>")
    return "".join(out) + ("</ul>" if in_list else "")


def render(run):
    alert = run.get("alert") or {}
    d = alert.get("detail") or {}
    case, sim = d.get("case") or {}, d.get("simulation") or {}
    h = [f"<!doctype html><html lang='id'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<title>Laporan OpsPilot - {_e(alert.get('title', run['id']))}</title><style>{CSS}</style></head><body><main>",
         "<div class='bar'><span>Laporan analisis OpsPilot</span><button onclick='window.print()'>Cetak / Simpan PDF</button></div>",
         f"<h1>{_e(alert.get('title') or run['alert_id'])}</h1>",
         f"<div class='meta'>Dibuat {dt.datetime.now().strftime('%d-%m-%Y %H:%M')} · investigasi {_e(run['id'])} · "
         f"{_e(run.get('model') or '-')} · {run.get('steps') or 0} panggilan AI · {run.get('tool_calls') or 0} panggilan tool"
         f" · {'terverifikasi' if run.get('verified') else 'belum terverifikasi'}</div>"]
    if case:
        h.append("<h2>Kasus</h2><div class='box'>")
        h.append(f"<p><b>{_e(case.get('title'))}</b> ({_e(case.get('date'))})"
                 + (f" · {_e(sim.get('company'))}" if sim.get("company") else "") + "</p>")
        h.append(f"<p>{_e(case.get('summary'))}</p>")
        if case.get("impact"):
            h.append(f"<p><b>Dampak operasional:</b> {_e(case['impact'])}</p>")
        if case.get("risk"):
            h.append(f"<p><b>Tingkat risiko:</b> {_e(case['risk'])}</p>")
        if case.get("sources"):
            h.append("<p><b>Sumber:</b></p><ol>" + "".join(
                f"<li><a href='{_e(s.get('url'))}'>{_e(s.get('title'))}</a></li>" for s in case["sources"]
                if str(s.get("url", "")).startswith("https://")) + "</ol>")
        h.append("</div>")
    h.append("<h2>Hasil analisis agent</h2>")
    h.append(f"<div class='box'>{_md(run.get('summary') or 'Investigasi belum selesai.')}</div>")
    if run.get("proposals"):
        h.append("<h2>Usulan tindakan</h2>")
        for n, p in enumerate(run["proposals"], 1):
            dt_ = p.get("detail") or {}
            h.append(f"<div class='box'><h3>{n}. {_e(p['label'])}</h3><div class='meta'>Status: {_e(STATUS.get(p['status'], p['status']))}</div>")
            if dt_.get("alasan"):
                h.append(f"<p><b>Kenapa:</b> {_e(dt_['alasan'])}</p>")
            if dt_.get("langkah"):
                h.append("<p><b>Langkah penyelesaian:</b></p><ol>" + "".join(f"<li>{_e(s)}</li>" for s in dt_["langkah"]) + "</ol>")
            meta = " · ".join(x for x in (f"<b>PIC:</b> {_e(dt_['pic'])}" if dt_.get("pic") else "",
                                          f"<b>Target:</b> {_e(dt_['target_waktu'])}" if dt_.get("target_waktu") else "") if x)
            if meta:
                h.append(f"<p>{meta}</p>")
            if dt_.get("hasil"):
                h.append(f"<p><b>Masalah dianggap selesai bila:</b> {_e(dt_['hasil'])}</p>")
            if p.get("result") and (p["result"].get("verification") or p["result"].get("error")):
                h.append(f"<p class='meta'>Hasil eksekusi: {_e(p['result'].get('verification') or p['result'].get('error'))}</p>")
            h.append("</div>")
    tools = [e["data"] for e in run.get("events", []) if e["kind"] == "tool"]
    if tools:
        h.append("<h2>Bukti yang dikumpulkan agent</h2><table><tr><th>ID</th><th>Tool</th><th>Keterangan</th></tr>")
        for t in tools:
            h.append(f"<tr><td>{_e(t.get('evidence_id', ''))}</td><td>{_e(t.get('name'))}</td><td>{_e(t.get('title', ''))}</td></tr>")
        h.append("</table>")
    h.append("<p class='meta' style='margin-top:24px'>Temuan berasal dari sumber berita publik, bukan data internal perusahaan. "
             "Semua tindakan menunggu persetujuan manusia.</p></main></body></html>")
    return "".join(h)
