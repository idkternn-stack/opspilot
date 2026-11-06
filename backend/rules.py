"""Diagnosis berbasis aturan (mode tanpa AI).

Dipakai hanya bila semua model AI sedang tidak bisa dipakai (kuota habis atau sibuk), supaya
OpsPilot tetap memberi diagnosis dan usulan. Hasilnya selalu diberi label "tanpa AI" di UI.
Bentuk keluarannya sama dengan jawaban AI sehingga diverifikasi dan diproses dengan cara yang sama.
"""
from . import tools


TEAM_BY_COMPLAINT = {"aplikasi": "IT & Product Engineering", "pembayaran": "Finance & Payment Operations",
                     "pengiriman": "Logistik", "produk": "Quality Control", "layanan": "Customer Service",
                     "keamanan_data": "IT Security"}
TEAM_BY_CATEGORY = {"keuangan": "Finance", "regulasi": "Legal & Compliance", "recall": "Quality Control",
                    "sistem_data": "IT Security", "stok_pasokan": "Supply Chain", "distribusi": "Logistik",
                    "harga_promo": "Pricing Team", "penutupan_toko": "Operations Manager", "permintaan": "Merchandising"}


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


def _enrich(out, d, ev):
    """Melengkapi tindakan aturan dengan langkah penyelesaian yang spesifik."""
    names = {r["store_id"]: r["store"] for r in ev.rows("stock_all")}
    if d.get("store_id") and d.get("store"):
        names.setdefault(int(d["store_id"]), d["store"])
    product = d.get("product")
    if not product:
        pid = next((a["args"].get("product_id") for a in out["tindakan"] if a["args"].get("product_id")), None)
        row = next((r for r in ev.rows("sales_by_product") if r.get("product_id") == pid), None)
        product = row["product"] if row else "produk terkait"
    refs = sorted({r for b in out["bukti"] for r in b.get("ref", [])})[:3]
    for a in out["tindakan"]:
        a.setdefault("ref", refs)
        tool, args = a["tool"], a["args"]
        if tool == "transfer_stock":
            src = names.get(args["from_store_id"], f"gerai {args['from_store_id']}")
            dst = names.get(args["to_store_id"], f"gerai {args['to_store_id']}")
            a.setdefault("langkah", [f"{src} menyiapkan {args['qty']} unit {product} dan membuat surat jalan",
                                     f"Kirim ke {dst} dengan armada antar-gerai hari ini",
                                     f"{dst} memeriksa barang, mengonfirmasi penerimaan, dan memajang produk",
                                     "Perbarui stok kedua gerai di sistem dan pantau penjualan 3 hari ke depan"])
            a.setdefault("pic", f"Supply Chain bersama Store Manager {src} dan {dst}")
            a.setdefault("hasil", f"{product} kembali tersedia/seimbang di {dst} tanpa membuat {src} di bawah reorder point")
        elif tool == "pause_promo":
            a.setdefault("langkah", [f"Nonaktifkan promo {product} di POS dan kanal online {d.get('store', '')}".strip(),
                                     "Kembalikan harga normal dan ganti label harga di rak",
                                     "Pricing Manager memeriksa siapa yang menyetujui promo dan memperbaiki prosedurnya",
                                     "Finance mencatat estimasi kerugian selama promo berjalan"])
            a.setdefault("hasil", f"Harga {product} kembali di atas harga pokok mulai hari ini")
    return out


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
        return _enrich(out, d, ev)
    if t in ("revenue_drop", "sales_drop") and pos:
        out["akar_masalah"] = "Data penjualan tidak lengkap karena sinkronisasi POS gagal."
        out["bukti"].append({"klaim": f"pos_sync_log berstatus {pos[0]['status']} pada {pos[0]['date']}", "ref": eid("pos_failed")})
        out["tindakan"].append({"tool": "create_ticket", "args": {"title": "Sinkronisasi ulang data POS", "priority": "HIGH",
                                "description": "Log POS gagal, data penjualan belum lengkap."}, "alasan": "SOP gangguan POS",
                                "pic": "IT Support", "langkah": ["IT Support memeriksa koneksi POS gerai ke server pusat",
                                "Jalankan sinkronisasi ulang transaksi tanggal yang gagal", "Cocokkan total transaksi dengan struk manual gerai",
                                "Tunda keputusan stok/harga sampai data lengkap"], "hasil": "Data penjualan tanggal yang hilang kembali lengkap"})
        return _enrich(out, d, ev)

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
                                    "description": f"Hubungi {late[0]['supplier']} atau supplier alternatif."}, "alasan": "SOP stockout",
                                    "pic": "Procurement", "langkah": [f"Hubungi {late[0]['supplier']} untuk kepastian jadwal kirim {top['product']} hari ini",
                                    "Bila keterlambatan lebih dari 3 hari, terbitkan PO darurat ke supplier alternatif",
                                    f"Kabari Store Manager {d.get('store')} soal ETA barang", "Catat keterlambatan ini untuk evaluasi kinerja supplier"],
                                    "hasil": f"Pengiriman {top['product']} ke {d.get('store')} punya jadwal pasti dan stok kembali normal"})
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
                                    "description": f"Permintaan di {c['store']} turun, stok menumpuk."}, "alasan": "SOP eskalasi",
                                    "pic": "Procurement bersama Merchandising", "langkah": [f"Tunda atau kurangi PO {d.get('product')} berikutnya untuk {c['store']}",
                                    "Merchandising mengevaluasi penyebab turunnya permintaan (harga, tren, kompetitor)",
                                    "Siapkan rencana penjualan stok lama (bundling atau pindah ke gerai yang masih laku)",
                                    "Pantau penjualan mingguan sampai kembali normal"],
                                    "hasil": f"Stok {d.get('product')} di {c['store']} turun ke level wajar dalam 2-4 minggu"})
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
                                    "description": "Procurement menghubungi supplier alternatif."}, "alasan": "SOP stockout poin 4",
                                    "pic": "Procurement", "langkah": [f"Konfirmasi ke {sup} penyebab keterlambatan dan jadwal kirim baru",
                                    f"Terbitkan PO darurat {d.get('product')} ke supplier alternatif untuk gerai terdampak",
                                    "Prioritaskan pengiriman ke gerai dengan stok nol", "Evaluasi kontrak dan kinerja supplier setelah kasus selesai"],
                                    "hasil": f"Semua gerai terdampak kembali punya stok {d.get('product')}"})
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
             "description": "Promo dihentikan, perlu cek persetujuan Pricing Manager."}, "alasan": "SOP pricing poin 2",
             "pic": "Pricing Team", "langkah": ["Telusuri siapa yang memasang promo dan apakah ada persetujuan Pricing Manager",
             "Perbaiki aturan di sistem promo agar diskon di atas 25% wajib persetujuan", "Laporkan total kerugian ke Finance"],
             "hasil": "Kesalahan diskon tidak terulang"}]

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
                                "description": "Lonjakan permintaan, stok menipis."}, "alasan": "SOP permintaan poin 3",
                                "pic": "Procurement", "langkah": [f"Tambah pesanan {d.get('product')} untuk {d.get('store')} sesuai penjualan terkini",
                                "Minta supplier mempercepat pengiriman", "Pantau penjualan harian selama lonjakan berlangsung"],
                                "hasil": f"Stok {d.get('product')} di {d.get('store')} cukup untuk minimal 7 hari"})

    elif t == "data_gap":
        failed = [r for r in ev.rows("pos_log") if r.get("status") != "OK"]
        out["akar_masalah"] = (f"Sinkronisasi POS {d.get('store')} gagal sehingga transaksi tidak tercatat."
                               if failed else f"Data penjualan {d.get('store')} hilang tanpa log POS gagal; perlu dicek.")
        for r in failed[:3]:
            out["bukti"].append({"klaim": f"pos_sync_log {r['date']} berstatus {r['status']}", "ref": eid("pos_log")})
        out["dampak"] = "Data belum lengkap, jadi tindakan stok atau harga ditunda."
        out["tindakan"] += [
            {"tool": "create_ticket", "args": {"title": f"Sinkronisasi ulang POS {d.get('store')}", "priority": "HIGH",
             "description": "Data penjualan hilang karena sinkronisasi gagal."}, "alasan": "SOP data POS poin 2",
             "pic": "IT Support", "langkah": [f"IT Support memeriksa koneksi POS {d.get('store')} ke server pusat",
             "Jalankan sinkronisasi ulang transaksi tanggal yang gagal", "Cocokkan hasil sinkron dengan struk manual gerai",
             "Pasang pemantauan agar gagal sinkron terdeteksi di hari yang sama"],
             "hasil": f"Data penjualan {d.get('store')} lengkap kembali"},
            {"tool": "send_notification", "args": {"to": f"Store Manager {d.get('store')}",
             "message": "Simpan bukti transaksi manual sampai data tersinkron."}, "alasan": "SOP data POS poin 4"}]
    elif t == "payment_anomaly":
        store = d.get("store")
        if d.get("issue") == "declined_but_charged":
            tot = (ev.by_key.get("refund_total") or {}).get("result") or {}
            ids = tot.get("payment_ids") or []
            out["akar_masalah"] = (f"{d.get('declined_charged')} transaksi kartu di {store} ditolak oleh bank/payment gateway, "
                                   "tetapi dana pelanggan tetap ditagih sehingga pelanggan membayar untuk transaksi yang gagal.")
            out["bukti"].append({"klaim": f"{d.get('declined_charged')} transaksi berstatus DECLINED tetapi charged=1 dan belum direfund, "
                                          f"total {_rupiah(d.get('declined_charged_amount'))}", "ref": eid("alert")})
            if ids:
                out["bukti"].append({"klaim": f"Daftar {tot['jumlah_transaksi']} transaksi yang harus direfund, total {tot['total_refund']}",
                                     "ref": eid("refund_total")})
            out["dampak"] = f"Pelanggan dirugikan {_rupiah(d.get('declined_charged_amount'))}; risiko komplain, chargeback, dan sanksi."
            out["tindakan"] += [
                {"tool": "refund_payments", "args": {"store_id": d["store_id"], "payment_ids": ids,
                 "reason": "Transaksi ditolak tetapi dana tetap ditagih"}, "alasan": "SOP pembayaran poin 2"},
                {"tool": "create_ticket", "args": {"title": f"Rekonsiliasi transaksi ditolak-tapi-ditagih {store}", "priority": "HIGH",
                 "description": "Transaksi DECLINED tetapi dana pelanggan terpotong."}, "alasan": "SOP pembayaran poin 3",
                 "pic": "Finance & Payment Operations", "langkah": [
                    "Cocokkan daftar transaksi dengan laporan settlement bank/payment gateway",
                    "Minta bank/payment gateway membatalkan penagihan yang tidak sah",
                    "Perbaiki logika status transaksi agar transaksi ditolak tidak diteruskan ke penagihan",
                    "Pantau transaksi kartu kredit 7 hari ke depan sampai tidak ada kasus baru"],
                 "hasil": "Tidak ada lagi transaksi DECLINED yang tertagih"},
                {"tool": "send_notification", "args": {"to": "Pelanggan terdampak",
                 "message": "Transaksi Anda ditolak tetapi sempat ditagih. Dana sedang dikembalikan dalam 1x24 jam."},
                 "alasan": "SOP pembayaran poin 4"}]
        else:
            out["akar_masalah"] = (f"Pembayaran di {store} gagal {d.get('recent_failed_pct')}% (normal {d.get('baseline_failed_pct')}%) "
                                   "karena gangguan payment gateway; dana pelanggan tidak terpotong.")
            out["bukti"].append({"klaim": f"Tingkat gagal bayar {d.get('recent_failed_pct')}% dibanding normal {d.get('baseline_failed_pct')}%",
                                 "ref": eid("alert")})
            out["dampak"] = "Penjualan hilang karena pelanggan tidak bisa membayar; tidak perlu refund karena dana tidak terpotong."
            out["tindakan"] += [
                {"tool": "create_ticket", "args": {"title": f"Gangguan payment gateway di {store}", "priority": "HIGH",
                 "description": "Transaksi gagal karena timeout payment gateway."}, "alasan": "SOP pembayaran poin 5",
                 "pic": "IT Payment", "langkah": ["Cek status dan log error payment gateway", "Alihkan ke gateway cadangan bila gangguan berlanjut",
                 "Uji transaksi di semua metode bayar setelah perbaikan"], "hasil": "Tingkat gagal bayar kembali di bawah 5%"},
                {"tool": "send_notification", "args": {"to": f"Store Manager dan Customer Service {store}",
                 "message": "Pembayaran sedang terganggu. Arahkan pelanggan ke metode bayar lain sementara."}, "alasan": "SOP pembayaran poin 5"}]

    elif t == "complaint_spike":
        cat, store = d.get("category", "layanan"), d.get("store")
        team = TEAM_BY_COMPLAINT.get(cat, "Customer Service")
        samples = ev.rows("cmp_samples")
        out["akar_masalah"] = (f"Keluhan {cat.replace('_', ' ')} di {store} melonjak dari {d.get('baseline_per_day')} menjadi "
                               f"{d.get('recent_per_day')} per hari" + (f"; isi keluhan dominan: \"{samples[0]['text']}\"." if samples else "."))
        out["bukti"].append({"klaim": f"Keluhan naik dari {d.get('baseline_per_day')} menjadi {d.get('recent_per_day')} per hari",
                             "ref": eid("alert")})
        if samples:
            out["bukti"].append({"klaim": f"Contoh keluhan: {samples[0]['text']}", "ref": eid("cmp_samples")})
        out["tindakan"] += [
            {"tool": "create_ticket", "args": {"title": f"Lonjakan keluhan {cat.replace('_', ' ')} di {store}", "priority": "HIGH",
             "description": samples[0]["text"] if samples else "Lonjakan keluhan pelanggan."}, "alasan": "SOP keluhan poin 2",
             "pic": team, "langkah": [f"{team} mereproduksi masalah yang dikeluhkan pelanggan",
             "Perbaiki penyebab utama dan uji ulang", "Customer Service menghubungi pelanggan yang mengeluh dengan solusi",
             "Pantau jumlah keluhan harian sampai kembali normal"], "hasil": f"Keluhan {cat.replace('_', ' ')} kembali ke level normal"},
            {"tool": "send_notification", "args": {"to": "Customer Service", "message": "Gunakan template jawaban resmi dan catat setiap keluhan terkait."},
             "alasan": "SOP keluhan poin 3"}]

    elif t == "order_delay":
        store = d.get("store")
        couriers = ev.rows("ord_courier")
        worst = couriers[0] if couriers else {}
        out["akar_masalah"] = (f"{d.get('late_orders')} dari {d.get('orders_due')} pesanan pelanggan {store} terlambat"
                               + (f", mayoritas lewat {worst['courier']} ({worst['terlambat']} pesanan)." if worst else "."))
        out["bukti"].append({"klaim": f"{d.get('late_orders')} dari {d.get('orders_due')} pesanan terlambat ({d.get('recent_late_pct')}%)",
                             "ref": eid("alert")})
        if worst:
            out["bukti"].append({"klaim": f"{worst['courier']}: {worst['terlambat']} dari {worst['pesanan']} pesanan terlambat",
                                 "ref": eid("ord_courier")})
        out["tindakan"] += [
            {"tool": "create_ticket", "args": {"title": f"Keterlambatan pengiriman {store}", "priority": "HIGH",
             "description": f"Kurir bermasalah: {worst.get('courier', '-')}"}, "alasan": "SOP pengiriman poin 2",
             "pic": "Logistik", "langkah": [f"Hubungi {worst.get('courier', 'kurir')} untuk status setiap paket terlambat",
             "Alihkan pesanan baru ke kurir lain sampai kinerja pulih", "Prioritaskan pengiriman ulang pesanan yang paling lama tertunda",
             "Tagih penalti SLA sesuai kontrak kurir"], "hasil": "Keterlambatan pesanan kembali di bawah 10%"},
            {"tool": "send_notification", "args": {"to": "Pelanggan dengan pesanan terlambat",
             "message": "Pesanan Anda terlambat. Kami sedang mempercepat pengiriman dan akan mengabari estimasi barunya."},
             "alasan": "SOP pengiriman poin 3"}]

    elif t == "case_report":
        case = d.get("case") or {}
        team = TEAM_BY_CATEGORY.get(case.get("category"), "Operations Manager")
        out["akar_masalah"] = case.get("summary") or case.get("title") or "-"
        out["bukti"].append({"klaim": case.get("summary") or case.get("title") or "-", "ref": eid("case")})
        out["dampak"] = case.get("impact") or ""
        steps = [str(m) for m in (case.get("mitigation") or [])][:5] or ["Verifikasi fakta kasus dengan tim internal",
                                                                         "Tetapkan langkah perbaikan dan PIC", "Laporkan progres ke manajemen"]
        out["tindakan"] += [
            {"tool": "create_ticket", "args": {"title": f"Tindak lanjut kasus: {(case.get('title') or '')[:80]}", "priority": "HIGH",
             "description": case.get("summary") or ""}, "alasan": "SOP kasus publik poin 2", "pic": team, "langkah": steps,
             "hasil": "Penyebab kasus ditangani dan tidak terulang"},
            {"tool": "send_notification", "args": {"to": "Manajemen dan Komunikasi Korporat",
             "message": f"Kasus publik: {case.get('title', '')}. Siapkan pernyataan resmi dan pantau perkembangan."},
             "alasan": "SOP kasus publik poin 3"}]
    else:
        out["akar_masalah"] = "Jenis anomali belum punya aturan diagnosis."
    if loss and not out["dampak"]:
        out["dampak"] = f"Estimasi pendapatan hilang {_rupiah(loss['lost_revenue_per_day'])} per hari."
    return _enrich(out, d, ev)
