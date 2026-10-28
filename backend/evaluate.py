"""Evaluasi otomatis OpsPilot.

Setiap skenario dibangun di database terpisah, detektor dijalankan, lalu agent menginvestigasi.
Hasil dinilai otomatis:
- terdeteksi       : detektor menghasilkan jenis anomali yang diharapkan
- diagnosis benar  : ringkasan agent memuat kata kunci akar masalah (setiap grup minimal satu kata)
- aksi tepat       : minimal satu tindakan yang diharapkan diusulkan (bila ada)
- aman             : tidak ada tindakan terlarang yang diusulkan (misal transfer stok saat toko tutup)

Jalankan:  python -m backend.evaluate            (semua skenario)
           python -m backend.evaluate --only data_pos_jakarta lonjakan_medan_mie
"""
import argparse
import datetime as dt
import json
import os
import statistics
import time

from . import agent, config, detect, scenarios


def grade(sc, summary, proposals):
    text = (summary or "").lower()
    expect = sc["expect"]
    diagnosis = all(any(k in text for k in group) for group in expect["keywords"])
    proposed = {p["tool"] for p in proposals}
    action = (not expect["actions_any"]) or bool(proposed & set(expect["actions_any"]))
    forbidden = sorted(proposed & set(expect["forbidden"]))
    return {"diagnosis_correct": diagnosis, "action_correct": action, "safe": not forbidden,
            "forbidden_proposed": forbidden, "passed": diagnosis and action and not forbidden}


def run_one(sid, workdir):
    sc = scenarios.SCENARIOS[sid]
    path = os.path.join(workdir, f"{sid}.db")
    scenarios.build(path, [sid])
    config.BUSINESS_DB_PATH = path
    row = {"scenario": sid, "title": sc["title"], "expected_alert": sc["expected_alert"]}
    alerts = [a for a in detect.detect_alerts() if a["type"] == sc["expected_alert"]]
    row["detected"] = bool(alerts)
    if not alerts:
        row.update(passed=False, error="Anomali tidak terdeteksi")
        return row
    sink = agent.MemorySink()
    started = time.time()
    try:
        result = agent.investigate(alerts[0], sink)
    except Exception as e:
        row.update(passed=False, error=str(e), duration_s=round(time.time() - started, 1))
        return row
    row.update(duration_s=round(time.time() - started, 1), steps=result["steps"], tool_calls=result["tool_calls"],
               verified=bool(result["verified"]), model=result["model"],
               proposals=[p["label"] for p in sink.proposals], summary=result["summary"])
    row.update(grade(sc, result["summary"], sink.proposals))
    return row


def pct(rows, key):
    return round(100 * sum(1 for r in rows if r.get(key)) / len(rows), 1) if rows else 0.0


def summarize(rows):
    ran = [r for r in rows if "steps" in r]
    mean = lambda k: round(statistics.mean(r[k] for r in ran), 1) if ran else None
    return {"scenarios": len(rows), "completed": len(ran), "detection_rate": pct(rows, "detected"),
            "diagnosis_accuracy": pct(rows, "diagnosis_correct"), "action_accuracy": pct(rows, "action_correct"),
            "safety_rate": pct(rows, "safe"), "pass_rate": pct(rows, "passed"), "verified_rate": pct(ran, "verified"),
            "avg_duration_s": mean("duration_s"), "avg_steps": mean("steps"), "avg_tool_calls": mean("tool_calls")}


def to_markdown(summary, rows, model, stamp):
    yn = lambda v: "ya" if v else "tidak"
    out = [f"# Hasil evaluasi OpsPilot ({stamp})", "", f"Model: `{model}`", "",
           "| Metrik | Nilai |", "|---|---|",
           f"| Skenario | {summary['scenarios']} (selesai {summary['completed']}) |",
           f"| Anomali terdeteksi | {summary['detection_rate']}% |",
           f"| Diagnosis akar masalah benar | {summary['diagnosis_accuracy']}% |",
           f"| Tindakan tepat | {summary['action_accuracy']}% |",
           f"| Tanpa tindakan terlarang | {summary['safety_rate']}% |",
           f"| Lulus semua kriteria | {summary['pass_rate']}% |",
           f"| Lolos verifikasi diri | {summary['verified_rate']}% |",
           f"| Rata-rata waktu investigasi | {summary['avg_duration_s']} detik |",
           f"| Rata-rata langkah / panggilan tool | {summary['avg_steps']} / {summary['avg_tool_calls']} |",
           "", "| Skenario | Terdeteksi | Diagnosis | Aksi | Aman | Lulus | Waktu (dtk) | Langkah |",
           "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['scenario']} | {yn(r.get('detected'))} | {yn(r.get('diagnosis_correct'))} | "
                   f"{yn(r.get('action_correct'))} | {yn(r.get('safe'))} | {yn(r.get('passed'))} | "
                   f"{r.get('duration_s', '-')} | {r.get('steps', '-')} |")
    errors = [r for r in rows if r.get("error")]
    if errors:
        out += ["", "Catatan error:"] + [f"- {r['scenario']}: {r['error']}" for r in errors]
    out += ["", "Penilaian diagnosis memakai pencocokan kata kunci pada ringkasan agent; "
                "detail kriteria per skenario ada di `backend/scenarios.py`."]
    return "\n".join(out) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Evaluasi otomatis OpsPilot")
    parser.add_argument("--only", nargs="*", help="ID skenario yang dijalankan (default: semua)")
    args = parser.parse_args()
    ids = args.only or list(scenarios.SCENARIOS)
    unknown = [i for i in ids if i not in scenarios.SCENARIOS]
    if unknown:
        raise SystemExit(f"Skenario tidak dikenal: {', '.join(unknown)}")
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY belum diatur. Isi file .env terlebih dahulu.")

    workdir = os.path.join(config.EVAL_DIR, "tmp")
    results_dir = os.path.join(config.EVAL_DIR, "results")
    os.makedirs(workdir, exist_ok=True)
    os.makedirs(results_dir, exist_ok=True)
    original_db = config.BUSINESS_DB_PATH
    rows = []
    try:
        for i, sid in enumerate(ids, 1):
            print(f"[{i}/{len(ids)}] {sid} ...", flush=True)
            row = run_one(sid, workdir)
            status = "LULUS" if row.get("passed") else "GAGAL"
            print(f"      {status} | {row.get('duration_s', '-')} dtk | {row.get('error', '')}", flush=True)
            rows.append(row)
    finally:
        config.BUSINESS_DB_PATH = original_db

    summary = summarize(rows)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    base = os.path.join(results_dir, f"eval_{stamp}")
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump({"model": config.MODEL, "summary": summary, "results": rows}, fh, ensure_ascii=False, indent=1)
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(to_markdown(summary, rows, config.MODEL, stamp))
    print("\n" + to_markdown(summary, rows, config.MODEL, stamp))
    print(f"Hasil disimpan di {base}.md dan .json")


if __name__ == "__main__":
    main()
