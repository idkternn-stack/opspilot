"""Diagnosis berbasis aturan (mode tanpa AI).

Dipakai hanya bila semua model AI sedang tidak bisa dipakai (kuota habis atau sibuk), supaya
OpsPilot tetap memberi diagnosis dan usulan. Hasilnya selalu diberi label "tanpa AI" di UI.
Bentuk keluarannya sama dengan jawaban AI sehingga diverifikasi dan diproses dengan cara yang sama.
"""
from . import tools


def _rupiah(x):
    return f"Rp{int(round(x or 0)):,}".replace(",", ".")


def _donor(stock_rows, exclude, pid):
    """Toko dengan kelebihan stok terbesar, beserta jumlah aman yang bisa ditransfer."""
    best = None
    for r in stock_rows:
        if r.get("store_id") in exclude:
            continue
        spare = (r.get("stock") or 0) - (r.get("reorder_point") or 0)
        if spare > 0 and (best is None or spare > best[1]):
            best = (r, spare)
    return best


def _transfer(ev, pid, to_sid, need):
    best = _donor(ev.rows("stock_all"), {to_sid}, pid)
    if not best:
        return None
    r, spare = best
    qty = int(min(spare, max(need, 20)))
    return {"tool": "transfer_stock", "args": {"product_id": pid, "from_store_id": r["store_id"], "to_store_id": to_sid,
                                               "qty": qty, "reason": "Transfer darurat sesuai SOP"},
            "alasan": f"{r['store']} punya stok {r['stock']} (reorder point {r['reorder_point']})"}, r


def diagnose(alert, ev):
    t, d = alert["type"], alert.get("detail", {})
    eid = lambda key: [ev.by_key[key]["id"]] if key in ev.by_key else []
    sop = sorted(ev.sop_files)[:2]
    out = {"rencana": ["Kumpulkan bukti otomatis sesuai jenis anomali", "Cocokkan pola dengan aturan diagnosis",
                       "Usulkan tindakan sesuai SOP"], "bukti": [], "sop": sop, "tindakan": [],
           "dampak": "", "alasan_tanpa_tindakan": ""}
    loss = ev.by_key.get("loss", {}).get("result") if "loss" in ev.by_key else None
    loss = loss if isinstance(loss, dict) and "error" not in loss else None

    events, pos = ev.rows("events"), ev.rows("pos_failed")
    if t in ("revenue_drop", "sales_drop") and events:
        e = events[0]
        out["akar_masalah"] = f"Penurunan terjadi karena kegiatan terjadwal: {e['event']} ({e['start_date']} s/d {e['end_date']})."
        out["bukti"].append({"klaim": f"store_events mencatat '{e['event']}' dari {e['start_date']} sampai {e['end_date']}",
                             "ref": eid("events")})
        out["alasan_tanpa_tindakan"] = "Kegiatan terjadwal yang sudah disetujui, bukan masalah operasional."
        return out
    if t in ("revenue_drop", "sales_drop") and pos:
        out["akar_masalah"] = "Data penjualan tidak lengkap karena sinkronisasi POS gagal."
        out["bukti"].append({"klaim": f"pos_sync_log berstatus {pos[0]['status']} pada {pos[0]['date']}", "ref": eid("pos_failed")})
        out["tindakan"].append({"tool": "create_ticket", "args": {"title": "Sinkronisasi ulang data POS", "priority": "HIGH",
                                "description": "Log POS gagal, data penjualan belum lengkap."}, "alasan": "SOP gangguan POS"})
        return out

    if t == "revenue_drop":
        top = (ev.rows("sales_by_product") or [{}])[0]
        dels = ev.rows("deliveries_open")
        inv = {r["product_id"]: r for r in ev.rows("inventory_store")}
        out["bukti"].append({"klaim": f"Pendapatan harian turun {d.get('drop_pct')}% dari {_rupiah(d.get('baseline_daily_revenue'))} "
                                      f"menjadi {_rupiah(d.get('recent_daily_revenue'))}", "ref": eid("alert")})
        pid = top.get("product_id")
        if pid and pid in inv:
            out["bukti"].append({"klaim": f"Penjualan {top['product']} turun dari {top['qty_baseline']} menjadi {top['qty_terkini']} "
                                          f"unit per hari, stok saat ini {inv[pid]['stock']}", "ref": eid("sales_by_product") + eid("inventory_store")})
        late = [x for x in dels if x.get("product_id") == pid]
        if late:
            out["akar_masalah"] = f"Stok {top['product']} habis karena pengiriman dari {late[0]['supplier']} terlambat."
            out["bukti"].append({"klaim": f"Pengiriman {top['product']} berstatus {late[0]['status']} sejak {late[0]['expected_date']}",
                                 "ref": eid("deliveries_open")})
            tr = _transfer(ev, pid, d["store_id"], int((top.get("qty_baseline") or 20) * 5))
            if tr:
                out["tindakan"].append(tr[0])
            out["tindakan"].append({"tool": "create_ticket", "args": {"title": f"Pengiriman {top['product']} terlambat", "priority": "HIGH",
                                    "description": f"Hubungi {late[0]['supplier']} atau supplier alternatif."}, "alasan": "SOP stockout"})
        else:
            out["akar_masalah"] = f"Penurunan terbesar ada pada {top.get('product', 'produk tertentu')}; penyebab pasti perlu dicek manual."
            out["tindakan"].append({"tool": "create_ticket", "args": {"title": f"Investigasi penurunan pendapatan {d.get('store')}",
                                    "priority": "MEDIUM", "description": "Penyebab tidak terlihat dari data stok, pengiriman, dan promo."},
                                    "alasan": "SOP eskalasi"})

    elif t == "sales_drop":
        delayed = [r for r in ev.rows("deliveries_product") if r.get("status") == "DELAYED"]
        suppliers = {r["supplier"] for r in delayed}
        for s in d.get("stores", []):
            out["bukti"].append({"klaim": f"Penjualan {d.get('product')} di {s['store']} turun dari {s['baseline_avg_qty']} "
                                          f"menjadi {s['recent_avg_qty']} unit per hari", "ref": eid("alert")})
        cover = ev.by_key.get("cover", {}).get("result") if "cover" in ev.by_key else None
        over = [c for c in (cover or []) if c["stok"] >= 3 * c["reorder_point"]]
        if not delayed and over:
            c = over[0]
            out["akar_masalah"] = f"Permintaan {d.get('product')} di {c['store']} turun sehingga stok menumpuk."
            out["bukti"].append({"klaim": f"Stok {d.get('product')} di {c['store']} {c['stok']} unit, reorder point {c['reorder_point']}",
                                 "ref": eid("cover")})
            out["bukti"].append({"klaim": "Tidak ada pengiriman berstatus DELAYED", "ref": eid("deliveries_product")})
            out["dampak"] = "Modal tertahan di stok berlebih dan risiko barang tidak laku."
            out["tindakan"].append({"tool": "create_ticket", "args": {"title": f"Tunda pesanan {d.get('product')}", "priority": "MEDIUM",
                                    "description": f"Permintaan di {c['store']} turun, stok menumpuk."}, "alasan": "SOP eskalasi"})
            first = int(d["stores"][0]["store_id"])
            others = [r for r in ev.rows("sales_by_store") if r.get("store_id") != first and r.get("qty_terkini")]
            if others:
                best = max(others, key=lambda r: r["qty_terkini"])
                qty = int(min(c["stok"] - c["reorder_point"], max(1, (best.get("qty_baseline") or 1) * 5)))
                if qty > 0:
                    out["tindakan"].append({"tool": "transfer_stock", "args": {"product_id": d["product_id"], "from_store_id": first,
                                            "to_store_id": best["store_id"], "qty": qty, "reason": "Pindahkan stok berlebih"},
                                            "alasan": f"{best['store']} penjualannya masih {best['qty_terkini']} unit/hari"})
        elif delayed:
            sup = sorted(suppliers)[0]
            out["akar_masalah"] = (f"Supplier {sup} terlambat mengirim {d.get('product')} ke {len(delayed)} toko."
                                   if len(delayed) > 1 else f"Stok {d.get('product')} habis karena pengiriman {sup} terlambat.")
            out["bukti"].append({"klaim": f"{len(delayed)} pengiriman dari {sup} berstatus DELAYED", "ref": eid("deliveries_product")})
            out["tindakan"].append({"tool": "create_ticket", "args": {"title": f"Supplier {sup} terlambat", "priority": "HIGH",
                                    "description": "Procurement menghubungi supplier alternatif."}, "alasan": "SOP stockout poin 4"})
            first = int(d["stores"][0]["store_id"]) if d.get("stores") else None
            exclude = {int(s["store_id"]) for s in d.get("stores", [])}
            best = _donor(ev.rows("stock_all"), exclude, d["product_id"]) if first else None
            if best:
                r, spare = best
                out["tindakan"].append({"tool": "transfer_stock", "args": {"product_id": d["product_id"], "from_store_id": r["store_id"],
                                        "to_store_id": first, "qty": int(min(spare, 100)), "reason": "Transfer darurat"},
                                        "alasan": f"{r['store']} punya stok {r['stock']}"})
        else:
            out["akar_masalah"] = f"Penjualan {d.get('product')} anjlok tanpa keterlambatan pengiriman yang tercatat."
            out["tindakan"].append({"tool": "create_ticket", "args": {"title": f"Cek penjualan {d.get('product')}", "priority": "MEDIUM",
                                    "description": "Penyebab perlu dicek manual."}, "alasan": "SOP eskalasi"})

    elif t == "discount_anomaly":
        price = (ev.rows("price") or [{}])[0]
        disc = d.get("recent_avg_discount") or 0
        net = (price.get("price") or 0) * (1 - disc)
        out["akar_masalah"] = f"Diskon {round(disc * 100)}% membuat harga jual {d.get('product')} di bawah harga pokok."
        out["bukti"].append({"klaim": f"Harga jual {_rupiah(price.get('price'))}, harga pokok {_rupiah(price.get('cost'))}, "
                                      f"diskon terkini {disc}", "ref": eid("price") + eid("alert")})
        out["bukti"].append({"klaim": f"Harga setelah diskon {_rupiah(net)}", "ref": eid("price")})
        if loss:
            out["dampak"] = f"Kerugian karena menjual di bawah harga pokok sekitar {_rupiah(loss['loss_from_selling_below_cost_per_day'])} per hari."
            out["bukti"].append({"klaim": f"Kerugian di bawah harga pokok {loss['loss_from_selling_below_cost_per_day']} per hari",
                                 "ref": eid("loss")})
        out["tindakan"] += [
            {"tool": "pause_promo", "args": {"store_id": d["store_id"], "product_id": d["product_id"], "reason": "Harga di bawah harga pokok"},
             "alasan": "SOP pricing poin 2"},
            {"tool": "create_ticket", "args": {"title": f"Kesalahan diskon {d.get('product')} di {d.get('store')}", "priority": "HIGH",
             "description": "Promo dihentikan, perlu cek persetujuan Pricing Manager."}, "alasan": "SOP pricing poin 2"}]

    elif t == "demand_spike":
        cover = ev.by_key.get("cover", {}).get("result", {}) if "cover" in ev.by_key else {}
        out["akar_masalah"] = f"Permintaan {d.get('product')} di {d.get('store')} naik {d.get('factor')}x sehingga stok menipis."
        out["bukti"].append({"klaim": f"Penjualan naik dari {d.get('baseline_avg_qty')} menjadi {d.get('recent_avg_qty')} unit per hari",
                             "ref": eid("alert")})
        if cover:
            out["bukti"].append({"klaim": f"Stok {cover['stok_saat_ini']} cukup untuk {cover['days_of_cover']} hari", "ref": eid("cover")})
        if cover and cover.get("days_of_cover", 99) < 3:
            tr = _transfer(ev, d["product_id"], d["store_id"], int((d.get("recent_avg_qty") or 20) * 4))
            if tr:
                out["tindakan"].append(tr[0])
        out["tindakan"].append({"tool": "create_ticket", "args": {"title": f"Tambah pesanan {d.get('product')}", "priority": "MEDIUM",
                                "description": "Lonjakan permintaan, stok menipis."}, "alasan": "SOP permintaan poin 3"})

    elif t == "data_gap":
        failed = [r for r in ev.rows("pos_log") if r.get("status") != "OK"]
        out["akar_masalah"] = (f"Sinkronisasi POS {d.get('store')} gagal sehingga transaksi tidak tercatat."
                               if failed else f"Data penjualan {d.get('store')} hilang tanpa log POS gagal; perlu dicek.")
        for r in failed[:3]:
            out["bukti"].append({"klaim": f"pos_sync_log {r['date']} berstatus {r['status']}", "ref": eid("pos_log")})
        out["dampak"] = "Data belum lengkap, jadi tindakan stok atau harga ditunda."
        out["tindakan"] += [
            {"tool": "create_ticket", "args": {"title": f"Sinkronisasi ulang POS {d.get('store')}", "priority": "HIGH",
             "description": "Data penjualan hilang karena sinkronisasi gagal."}, "alasan": "SOP data POS poin 2"},
            {"tool": "send_notification", "args": {"to": f"Store Manager {d.get('store')}",
             "message": "Simpan bukti transaksi manual sampai data tersinkron."}, "alasan": "SOP data POS poin 4"}]
    else:
        out["akar_masalah"] = "Jenis anomali belum punya aturan diagnosis."
    if loss and not out["dampak"]:
        out["dampak"] = f"Estimasi pendapatan hilang {_rupiah(loss['lost_revenue_per_day'])} per hari."
    return out
