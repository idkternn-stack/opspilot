"""Data retail sintetis dan skenario anomali.

Setiap skenario punya akar masalah yang diketahui, sehingga bisa dipakai untuk:
- demo (beberapa skenario sekaligus di satu database), dan
- evaluasi (satu skenario per database, hasil agent dinilai otomatis).

Jendela waktu: data 30 hari terakhir. "Periode terkini" = 3 hari terakhir,
"baseline" = hari ke-8 sampai ke-30 ke belakang.
"""
import datetime as dt
import json
import os
import random
import re
import unicodedata
import sqlite3

STORES = [(1, "Jakarta Pusat", "Jakarta"), (2, "Bandung Dago", "Bandung"),
          (3, "Surabaya Tunjungan", "Surabaya"), (4, "Medan Kota", "Medan")]
# id, nama, kategori, harga jual, harga pokok, rata-rata qty harian
PRODUCTS = [(1, "Minyak Goreng 2L", "Sembako", 38000, 31000, 40),
            (2, "Beras 5kg", "Sembako", 68000, 60000, 25),
            (3, "Gula 1kg", "Sembako", 16000, 13500, 30),
            (4, "Kopi Sachet Box", "Minuman", 12000, 8500, 35),
            (5, "Susu UHT 1L", "Minuman", 20000, 16000, 30),
            (6, "Mie Instan", "Makanan", 3500, 2800, 80),
            (7, "Sabun Mandi", "Perawatan", 5000, 3500, 40),
            (8, "Teh Botol", "Minuman", 6000, 4200, 50)]
REORDER_POINT = 50
DEFAULT_SUPPLIER = "PT Sumber Pangan"
DAYS = 30

# expect.keywords: setiap grup minimal satu kata harus muncul di ringkasan agent
# expect.actions_any: minimal satu tool aksi ini harus diusulkan
# expect.forbidden: tool aksi yang TIDAK boleh diusulkan (tindakan tidak perlu)
SCENARIOS = {
    "stockout_bandung_minyak": {
        "kind": "stockout_delay", "store": 2, "product": 1, "donor": 1,
        "title": "Stok Minyak Goreng habis di Bandung karena pengiriman terlambat",
        "expected_alert": "revenue_drop",
        "expect": {"keywords": [["stok", "stockout", "habis", "kosong"], ["terlambat", "delay", "pengiriman"]],
                   "actions_any": ["transfer_stock"], "forbidden": ["pause_promo"]},
    },
    "stockout_medan_susu": {
        "kind": "stockout_delay", "store": 4, "product": 5, "donor": 3,
        "title": "Stok Susu UHT habis di Medan karena pengiriman terlambat",
        "expected_alert": "sales_drop",
        "expect": {"keywords": [["stok", "stockout", "habis", "kosong"], ["terlambat", "delay", "pengiriman"]],
                   "actions_any": ["transfer_stock"], "forbidden": ["pause_promo"]},
    },
    "discount_surabaya_beras": {
        "kind": "wrong_discount", "store": 3, "product": 2, "discount": 0.4,
        "title": "Diskon 40% Beras di Surabaya membuat harga jual di bawah harga pokok",
        "expected_alert": "discount_anomaly",
        "expect": {"keywords": [["diskon", "promo"], ["harga pokok", "di bawah", "rugi", "kerugian", "margin", "cost"]],
                   "actions_any": ["pause_promo"], "forbidden": ["transfer_stock"]},
    },
    "discount_jakarta_kopi": {
        "kind": "wrong_discount", "store": 1, "product": 4, "discount": 0.5,
        "title": "Diskon 50% Kopi di Jakarta membuat harga jual di bawah harga pokok",
        "expected_alert": "discount_anomaly",
        "expect": {"keywords": [["diskon", "promo"], ["harga pokok", "di bawah", "rugi", "kerugian", "margin", "cost"]],
                   "actions_any": ["pause_promo"], "forbidden": ["transfer_stock"]},
    },
    "supplier_gula_multi_toko": {
        "kind": "supplier_delay", "product": 3, "stores": [1, 4], "supplier": "CV Gula Manis",
        "title": "Supplier CV Gula Manis terlambat mengirim Gula ke dua toko",
        "expected_alert": "sales_drop",
        "expect": {"keywords": [["cv gula manis", "supplier", "pemasok"], ["terlambat", "delay", "pengiriman"]],
                   "actions_any": ["create_ticket"], "forbidden": ["pause_promo"]},
    },
    "lonjakan_medan_mie": {
        "kind": "demand_spike", "store": 4, "product": 6, "factor": 3, "donor": 3,
        "title": "Permintaan Mie Instan di Medan melonjak 3x, stok tinggal sedikit",
        "expected_alert": "demand_spike",
        "expect": {"keywords": [["lonjakan", "melonjak", "naik", "meningkat", "kenaikan", "spike"], ["stok", "persediaan"]],
                   "actions_any": ["transfer_stock", "create_ticket"], "forbidden": ["pause_promo"]},
    },
    "permintaan_turun_jakarta_sabun": {
        "kind": "demand_drop", "store": 1, "product": 7,
        "title": "Permintaan Sabun Mandi di Jakarta turun tajam, stok menumpuk",
        "expected_alert": "sales_drop",
        "expect": {"keywords": [["permintaan", "turun", "menurun", "lesu", "melemah"], ["stok", "menumpuk", "berlebih", "overstock", "persediaan"]],
                   "actions_any": ["create_ticket", "transfer_stock"], "forbidden": ["pause_promo"]},
    },
    "data_pos_jakarta": {
        "kind": "data_gap", "store": 1, "days": [2, 3],
        "title": "Data penjualan Jakarta hilang 2 hari karena sinkronisasi POS gagal",
        "expected_alert": "data_gap",
        "expect": {"keywords": [["pos", "sinkron", "sync"]],
                   "actions_any": ["create_ticket"], "forbidden": ["transfer_stock", "pause_promo"]},
    },
    "tagihan_ganda_surabaya": {
        "kind": "payment_error", "store": 3,
        "title": "Transaksi kartu ditolak tetapi dana pelanggan tetap ditagih di Surabaya",
        "expected_alert": "payment_anomaly",
        "expect": {"keywords": [["ditolak", "declined"], ["ditagih", "tagihan", "terpotong", "dibebankan", "charged"]],
                   "actions_any": ["refund_payments"], "forbidden": ["transfer_stock", "pause_promo"]},
    },
    "pembayaran_gagal_bandung": {
        "kind": "payment_outage", "store": 2,
        "title": "Pembayaran gagal massal di Bandung karena gangguan payment gateway",
        "expected_alert": "payment_anomaly",
        "expect": {"keywords": [["gagal", "failed", "timeout"], ["pembayaran", "payment", "gateway"]],
                   "actions_any": ["create_ticket"], "forbidden": ["refund_payments", "transfer_stock", "pause_promo"]},
    },
    "keluhan_aplikasi_jakarta": {
        "kind": "complaint_spike", "store": 1, "category": "aplikasi",
        "title": "Keluhan aplikasi error di Jakarta melonjak",
        "expected_alert": "complaint_spike",
        "expect": {"keywords": [["keluhan", "komplain"], ["aplikasi", "app", "error"]],
                   "actions_any": ["create_ticket"], "forbidden": ["transfer_stock", "pause_promo", "refund_payments"]},
    },
    "pesanan_terlambat_medan": {
        "kind": "order_delay", "store": 4,
        "title": "Pesanan pelanggan di Medan terlambat karena satu mitra kurir bermasalah",
        "expected_alert": "order_delay",
        "expect": {"keywords": [["terlambat", "delay"], ["kurir", "logistik", "pengiriman"]],
                   "actions_any": ["create_ticket"], "forbidden": ["pause_promo", "refund_payments"]},
    },
    "toko_tutup_surabaya": {
        "kind": "store_closure", "store": 3, "days": [1, 2],
        "title": "Pendapatan Surabaya turun karena toko tutup terjadwal untuk renovasi",
        "expected_alert": "revenue_drop",
        "expect": {"keywords": [["renovasi", "tutup"]],
                   "actions_any": [], "forbidden": ["transfer_stock", "pause_promo"]},
    },
}

# Data demo dimulai bersih: anomali hanya muncul dari simulasi kasus perusahaan nyata.
DEMO = []

# Set contoh beberapa anomali sekaligus, dipakai tes offline.
EXAMPLE_SET = ["stockout_bandung_minyak", "discount_surabaya_beras", "supplier_gula_multi_toko",
        "lonjakan_medan_mie", "data_pos_jakarta"]

MAX_NAME = 40


class CatalogError(ValueError):
    pass


def normalize_catalog(raw, company=""):
    """Memvalidasi katalog dari AI: tepat 4 toko dan 8 produk dengan harga masuk akal."""
    if not isinstance(raw, dict):
        raise CatalogError("Katalog bukan objek.")
    clean = lambda v: re.sub(r"\s+", " ", re.sub(r"[<>{}`\"]", "", str(v or ""))).strip()[:MAX_NAME]
    stores, products = [], []
    for i, t in enumerate((raw.get("toko") or [])[:4], 1):
        name = clean(t.get("nama") if isinstance(t, dict) else t)
        city = clean(t.get("kota") if isinstance(t, dict) else "") or name
        if name:
            stores.append((i, name, city))
    for i, p in enumerate((raw.get("produk") or [])[:8], 1):
        if not isinstance(p, dict):
            continue
        name, cat = clean(p.get("nama")), clean(p.get("kategori")) or "Umum"
        try:
            price = float(p.get("harga"))
        except (TypeError, ValueError):
            continue
        try:
            cost = float(p.get("harga_pokok"))
        except (TypeError, ValueError):
            cost = 0
        try:
            qty = float(p.get("qty_harian"))
        except (TypeError, ValueError):
            qty = 10
        if not name or not (0 < price <= 5e9):
            continue
        if not 0 < cost < price:  # harga pokok tidak wajar (mis. voucher): pakai perkiraan 80% harga
            cost = price * 0.8
        products.append((i, name, cat, round(price), round(cost), int(min(max(qty, 3), 120))))
    for i in range(len(stores) + 1, 5) if stores else ():  # gerai kurang: tambahkan gerai online
        stores.append((i, f"Gerai online {i}", "Online"))
    if len(stores) < 4 or len(products) < 8:
        raise CatalogError("Katalog butuh tepat 4 toko dan 8 produk yang valid.")
    company = clean(company or raw.get("perusahaan")) or "Perusahaan"
    ascii_name = unicodedata.normalize("NFKD", company).encode("ascii", "ignore").decode()
    key = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:30] or "perusahaan"
    return {"company": company, "key": key, "stores": stores, "products": products}


def load_catalog(obj, company=""):
    """Menerima katalog yang sudah dinormalisasi (dari hasil riset) atau mentah (dari AI)."""
    if isinstance(obj, dict) and "stores" in obj and "products" in obj:
        stores, products = [tuple(s) for s in obj["stores"]], [tuple(p) for p in obj["products"]]
        if len(stores) != 4 or len(products) != 8:
            raise CatalogError("Katalog butuh tepat 4 toko dan 8 produk.")
        return {"company": obj.get("company") or company, "key": obj.get("key") or "perusahaan",
                "stores": stores, "products": products}
    return normalize_catalog(obj, company)


DEFAULT_CATALOG = {"company": "Toko sembako (default)", "key": "default", "stores": STORES, "products": PRODUCTS}

BUSINESS_SCHEMA = """
CREATE TABLE stores(id INTEGER PRIMARY KEY, name TEXT, city TEXT);
CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, category TEXT, price REAL, cost REAL, base_qty INTEGER);
CREATE TABLE sales(date TEXT, store_id INT, product_id INT, qty INT, discount REAL);
CREATE TABLE inventory(store_id INT, product_id INT, stock INT, reorder_point INT);
CREATE TABLE deliveries(store_id INT, product_id INT, supplier TEXT, expected_date TEXT, received_date TEXT, status TEXT);
CREATE TABLE promotions(store_id INT, product_id INT, discount REAL, active INT, start_date TEXT);
CREATE TABLE pos_sync_log(store_id INT, date TEXT, status TEXT, note TEXT);
CREATE TABLE store_events(store_id INT, start_date TEXT, end_date TEXT, event TEXT);
CREATE TABLE stock_transfers(id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INT, from_store_id INT, to_store_id INT, qty INT, reason TEXT, created_at TEXT);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE payments(id INTEGER PRIMARY KEY, date TEXT, store_id INT, method TEXT, amount REAL, status TEXT, charged INT, refunded INT, note TEXT);
CREATE TABLE complaints(id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, store_id INT, channel TEXT, category TEXT, text TEXT);
CREATE TABLE orders(id INTEGER PRIMARY KEY, order_date TEXT, store_id INT, product_id INT, qty INT, promised_date TEXT, delivered_date TEXT, status TEXT, courier TEXT);
CREATE TABLE tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, priority TEXT, description TEXT, created_at TEXT);
CREATE TABLE notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT, message TEXT, created_at TEXT);
"""


def build(path, scenario_ids=None, rnd_seed=7, today=None, catalog=None):
    """Membuat database bisnis baru di `path` dengan skenario yang dipilih.

    `catalog` (hasil normalize_catalog) mengganti toko dan produk default dengan katalog simulasi perusahaan.
    """
    scenario_ids = DEMO if scenario_ids is None else scenario_ids
    cat = catalog or DEFAULT_CATALOG
    STORES, PRODUCTS = cat["stores"], cat["products"]
    generic = catalog is not None
    reorder = {p[0]: (max(2, round(p[5] * 1.25)) if generic else REORDER_POINT) for p in PRODUCTS}
    today = today or dt.date.today()
    rnd = random.Random(rnd_seed)
    day = lambda days_ago: (today - dt.timedelta(days=days_ago)).isoformat()

    sales = {}
    for d in range(DAYS, 0, -1):
        for s, *_ in STORES:
            for p, *_rest in PRODUCTS:
                base = _rest[-1]
                sales[(d, s, p)] = [round(base * rnd.uniform(0.8, 1.2)), 0.0]
    base_of = {p[0]: p[5] for p in PRODUCTS}
    inventory = {(s, p): (rnd.randint(base_of[p] * 2, round(base_of[p] * 7.5)) if generic else rnd.randint(80, 300))
                 for s, *_ in STORES for p, *_ in PRODUCTS}
    supplier = "Pemasok utama (simulasi)" if generic else DEFAULT_SUPPLIER
    deliveries = {(s, p): [supplier, day(10), day(10), "DELIVERED"] for s, *_ in STORES for p, *_ in PRODUCTS}
    promotions, events = [], []
    missing, pos_failed = set(), {}

    for sid in scenario_ids:
        sc = SCENARIOS[sid]
        kind = sc["kind"]
        if kind == "stockout_delay":
            s, p = sc["store"], sc["product"]
            for d in range(1, 5):
                sales[(d, s, p)][0] = 0
            inventory[(s, p)] = 0
            inventory[(sc["donor"], p)] = 300
            deliveries[(s, p)] = [supplier, day(6), None, "DELAYED"]
        elif kind == "wrong_discount":
            s, p = sc["store"], sc["product"]
            for d in range(1, 4):
                sales[(d, s, p)][1] = sc["discount"]
            promotions.append((s, p, sc["discount"], 1, day(3)))
        elif kind == "supplier_delay":
            p = sc["product"]
            for s, *_ in STORES:
                if s in sc["stores"]:
                    for d in range(1, 5):
                        sales[(d, s, p)][0] = 0
                    inventory[(s, p)] = 0
                    deliveries[(s, p)] = [sc["supplier"], day(5), None, "DELAYED"]
                else:
                    deliveries[(s, p)] = [sc["supplier"], day(10), day(10), "DELIVERED"]
            others = [s for s, *_ in STORES if s not in sc["stores"]]
            inventory[(others[0], p)] = 280
        elif kind == "demand_spike":
            s, p = sc["store"], sc["product"]
            for d in range(1, 5):
                sales[(d, s, p)][0] *= sc["factor"]
            inventory[(s, p)] = 60
            inventory[(sc["donor"], p)] = 300
        elif kind == "demand_drop":
            s, p = sc["store"], sc["product"]
            for d in range(1, 5):
                sales[(d, s, p)][0] = round(sales[(d, s, p)][0] * 0.25)
            inventory[(s, p)] = max(inventory[(s, p)], base_of[p] * 15)
        elif kind == "data_gap":
            for d in sc["days"]:
                missing.add((sc["store"], d))
                pos_failed[(sc["store"], d)] = "Koneksi POS ke server pusat terputus, transaksi tidak tersinkron"
        elif kind == "store_closure":
            s = sc["store"]
            for d in sc["days"]:
                for p, *_ in PRODUCTS:
                    sales[(d, s, p)][0] = 0
            events.append((s, day(max(sc["days"])), day(min(sc["days"])),
                           "Toko tutup sementara untuk renovasi (terjadwal, disetujui Regional Manager)"))
        elif kind not in EXTRA_KINDS:
            raise ValueError(f"Jenis skenario tidak dikenal: {kind}")

    if os.path.exists(path):
        os.remove(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(BUSINESS_SCHEMA)
        con.executemany("INSERT INTO stores VALUES (?,?,?)", STORES)
        con.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", PRODUCTS)
        con.executemany("INSERT INTO sales VALUES (?,?,?,?,?)",
                        [(day(d), s, p, q, disc) for (d, s, p), (q, disc) in sales.items() if (s, d) not in missing])
        con.executemany("INSERT INTO inventory VALUES (?,?,?,?)",
                        [(s, p, stock, reorder[p]) for (s, p), stock in inventory.items()])
        con.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", [(s, p, *v) for (s, p), v in deliveries.items()])
        con.executemany("INSERT INTO promotions VALUES (?,?,?,?,?)", promotions)
        con.executemany("INSERT INTO store_events VALUES (?,?,?,?)", events)
        con.executemany("INSERT INTO pos_sync_log VALUES (?,?,?,?)",
                        [(s, day(d), "FAILED" if (s, d) in pos_failed else "OK", pos_failed.get((s, d), ""))
                         for d in range(DAYS, 0, -1) for s, *_ in STORES])
        _customer_data(con, STORES, PRODUCTS, random.Random(rnd_seed + 1), day)
        for sid in scenario_ids:
            sc = SCENARIOS[sid]
            if sc["kind"] in EXTRA_KINDS:
                apply_extra(con, sc["kind"], sc, "", today)
        con.execute("INSERT INTO meta VALUES ('catalog', ?)", (json.dumps(cat, ensure_ascii=False),))
        con.commit()
    finally:
        con.close()
    return path


# ---------------------------------------------------------------- data pelanggan
PAY_METHODS = ["Kartu kredit", "Kartu debit", "E-wallet", "Transfer bank", "Virtual account"]
PAY_PER_DAY, ORDERS_PER_DAY = 10, 6
COURIERS = ["Kurir internal", "JNE", "SiCepat", "J&T"]
SIM_COURIER = "Mitra kurir (simulasi)"
COMPLAINT_CATEGORIES = {"aplikasi": "aplikasi/website error", "pembayaran": "pembayaran", "pengiriman": "pengiriman pesanan",
                        "produk": "kualitas produk", "layanan": "layanan pelanggan", "keamanan_data": "keamanan akun/data"}
COMPLAINT_TEXTS = {
    "aplikasi": ["Aplikasi tidak bisa dibuka", "Gagal login berkali-kali", "Halaman checkout error"],
    "pembayaran": ["Pembayaran gagal tapi saldo terpotong", "Pembayaran tidak terkonfirmasi", "Tagihan ganda"],
    "pengiriman": ["Pesanan belum sampai", "Status pengiriman tidak bergerak", "Paket terlambat"],
    "produk": ["Produk rusak saat diterima", "Produk tidak sesuai deskripsi", "Produk kedaluwarsa"],
    "layanan": ["CS lambat merespons", "Keluhan tidak ditindaklanjuti", "Petugas gerai kurang ramah"],
    "keamanan_data": ["Akun diakses orang lain", "Menerima pesan penipuan atas nama toko", "Data pribadi bocor"],
}
CHANNELS = ["Aplikasi", "Call center", "Email", "Media sosial"]
EXTRA_KINDS = ("payment_error", "payment_outage", "complaint_spike", "order_delay")


def _customer_data(con, stores, products, rnd, day):
    """Transaksi pembayaran, pesanan pelanggan, dan keluhan normal selama 30 hari (tanpa anomali)."""
    pay, orders, comp = [], [], []
    pid_n = oid_n = 0
    for d in range(DAYS, 0, -1):
        for s, *_ in stores:
            for _ in range(PAY_PER_DAY):
                pid_n += 1
                prod = rnd.choice(products)
                ok = rnd.random() > 0.03
                pay.append((pid_n, day(d), s, rnd.choice(PAY_METHODS), prod[3] * rnd.randint(1, 3),
                            "SUCCESS" if ok else "FAILED", 1 if ok else 0, 0, "" if ok else "Saldo/limit tidak cukup"))
            for _ in range(ORDERS_PER_DAY):
                oid_n += 1
                prod = rnd.choice(products)
                promised = d - 2
                if promised <= 0:
                    status, delivered = "DIPROSES", None
                elif rnd.random() < 0.05:
                    status, delivered = "TERLAMBAT", day(promised - 1) if promised > 1 else None
                else:
                    status, delivered = "DITERIMA", day(promised)
                orders.append((oid_n, day(d), s, prod[0], rnd.randint(1, 3), day(promised), delivered, status,
                               rnd.choice(COURIERS)))
            for _ in range(rnd.choice([0, 0, 1, 1, 2])):
                cat = rnd.choice(list(COMPLAINT_TEXTS))
                comp.append((day(d), s, rnd.choice(CHANNELS), cat, rnd.choice(COMPLAINT_TEXTS[cat])))
    con.executemany("INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?)", pay)
    con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?)", orders)
    con.executemany("INSERT INTO complaints(date, store_id, channel, category, text) VALUES (?,?,?,?,?)", comp)


def apply_extra(con, kind, p, note="", today=None, case_text=""):
    """Pola masalah pelanggan: pembayaran, keluhan, dan pesanan terlambat (3 hari terakhir)."""
    today = today or dt.date.today()
    d = lambda n: (today - dt.timedelta(days=n)).isoformat()
    s = p["store"]
    if kind == "payment_error":
        for n in (1, 2, 3):
            con.execute("""UPDATE payments SET status = 'DECLINED', charged = 1, refunded = 0, method = 'Kartu kredit',
                           note = 'Otorisasi ditolak bank, tetapi dana pelanggan tetap ditagih'
                           WHERE id IN (SELECT id FROM payments WHERE store_id = ? AND date = ? AND status = 'SUCCESS'
                           ORDER BY id LIMIT 5)""", (s, d(n)))
    elif kind == "payment_outage":
        for n in (1, 2, 3):
            con.execute("""UPDATE payments SET status = 'FAILED', charged = 0, note = 'Timeout dari payment gateway'
                           WHERE id IN (SELECT id FROM payments WHERE store_id = ? AND date = ? AND status = 'SUCCESS'
                           ORDER BY id LIMIT 5)""", (s, d(n)))
    elif kind == "complaint_spike":
        cat = p.get("category") if p.get("category") in COMPLAINT_TEXTS else "layanan"
        texts = ([f"{prefix}{case_text}" for prefix in ("", "Pelanggan melapor: ", "Keluhan berulang: ", "Mohon segera dibantu: ")]
                 if case_text else COMPLAINT_TEXTS[cat])
        rows = [(d(n), s, CHANNELS[i % len(CHANNELS)], cat, texts[i % len(texts)]) for n in (1, 2, 3) for i in range(8)]
        con.executemany("INSERT INTO complaints(date, store_id, channel, category, text) VALUES (?,?,?,?,?)", rows)
    elif kind == "order_delay":
        con.execute("""UPDATE orders SET status = 'TERLAMBAT', delivered_date = NULL, courier = ?
                       WHERE store_id = ? AND promised_date BETWEEN ? AND ? AND id % 4 != 0""", (SIM_COURIER, s, d(3), d(1)))
    else:
        raise ValueError(f"Jenis skenario tidak dikenal: {kind}")


def outdated(path):
    """True bila database toko dibuat versi lama (belum punya tabel pembayaran, pesanan, keluhan)."""
    try:
        con = sqlite3.connect(path)
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            con.close()
    except sqlite3.Error:
        return True
    return not {"payments", "orders", "complaints"} <= names


def current_catalog(path):
    """Katalog yang sedang dipakai database bisnis (default bila belum ada meta)."""
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute("SELECT value FROM meta WHERE key = 'catalog'").fetchone()
        finally:
            con.close()
        return json.loads(row[0]) if row else DEFAULT_CATALOG
    except sqlite3.Error:
        return DEFAULT_CATALOG
