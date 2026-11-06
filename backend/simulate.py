"""Menjadikan temuan riset perusahaan nyata sebagai anomali yang dianalisis agent.

Judul dan isi anomali sama dengan masalah aslinya (dari sumber berita publik). Agent lalu menalar akar
masalah dari sumber dan SOP, dan mengusulkan tindakan penyelesaian yang menunggu persetujuan manusia.
"""
import threading

from . import store

SIM_LOCK = threading.Lock()


class SimulationError(ValueError):
    pass


def simulate_issue(research_id, issue_id, engine):
    """Menjadikan temuan riset sebagai anomali yang SAMA dengan masalah aslinya, lalu agent menganalisisnya."""
    with SIM_LOCK:
        rec = store.get_research(research_id)
        if not rec or rec["status"] != "done":
            raise SimulationError("Riset belum selesai atau tidak ditemukan.")
        issue = next((i for i in rec["result"]["issues"] if i["id"] == issue_id), None)
        if not issue:
            raise SimulationError("Temuan tidak ditemukan.")
        if issue.get("simulated"):
            return issue["simulated"]
        if issue.get("relevant") is False:
            raise SimulationError("Temuan ini bukan masalah operasional perusahaan, jadi tidak dianalisis.")
        company = rec["result"]["company"].get("name") or rec["company"]
        problem = (issue.get("problem") or issue["title"]).strip().rstrip(".")
        alert = {"id": f"case:{research_id}:{issue_id}", "type": "case_report", "title": f"{problem} ({company})",
                 "detail": {"company": company, "simulation": {
                                "company": company, "case": issue["title"], "case_date": issue["date"], "problem": problem,
                                "case_category": issue.get("category_label", ""),
                                "public_sources": [s["domain"] or s["title"] for s in issue["sources"]][:3]},
                            "case": {"title": issue["title"], "date": issue["date"], "category": issue["category"],
                                     "summary": issue["summary"], "impact": issue.get("impact", ""),
                                     "risk": issue.get("risk"), "mitigation": issue.get("mitigation", []),
                                     "confidence": issue.get("confidence"),
                                     "sources": [{"title": s["title"], "url": s["url"]} for s in issue["sources"]][:5]}}}
        store.sync_alerts([alert])
        store.update_alert_meta(alert)
        try:
            run_id = engine.enqueue(alert["id"], trigger="simulasi")
        except ValueError:
            run_id = store.active_run_for(alert["id"])
        info = {"alert_id": alert["id"], "run_id": run_id, "alert_title": alert["title"]}
        store.set_issue_simulated(research_id, issue_id, info)
        return info
