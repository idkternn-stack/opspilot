"""Data retail sintetis dan skenario anomali.

Setiap skenario punya akar masalah yang diketahui, sehingga bisa dipakai untuk:
- demo (beberapa skenario sekaligus di satu database), dan
- evaluasi (satu skenario per database, hasil agent dinilai otomatis).

Jendela waktu: data 30 hari terakhir. "Periode terkini" = 3 hari terakhir,
"baseline" = hari ke-8 sampai ke-30 ke belakang.
"""
import datetime as dt
import os
import random
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
    "data_pos_jakarta": {
        "kind": "data_gap", "store": 1, "days": [2, 3],
        "title": "Data penjualan Jakarta hilang 2 hari karena sinkronisasi POS gagal",
        "expected_alert": "data_gap",
        "expect": {"keywords": [["pos", "sinkron", "sync"]],
                   "actions_any": ["create_ticket"], "forbidden": ["transfer_stock", "pause_promo"]},
    },
    "toko_tutup_surabaya": {
        "kind": "store_closure", "store": 3, "days": [1, 2],
        "title": "Pendapatan Surabaya turun karena toko tutup terjadwal untuk renovasi",
        "expected_alert": "revenue_drop",
        "expect": {"keywords": [["renovasi", "tutup"]],
                   "actions_any": [], "forbidden": ["transfer_stock", "pause_promo"]},
    },
}

DEMO = ["stockout_bandung_minyak", "discount_surabaya_beras", "supplier_gula_multi_toko",
        "lonjakan_medan_mie", "data_pos_jakarta"]

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
CREATE TABLE tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, priority TEXT, description TEXT, created_at TEXT);
CREATE TABLE notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT, message TEXT, created_at TEXT);
"""


def build(path, scenario_ids=None, rnd_seed=7, today=None):
    """Membuat database bisnis baru di `path` dengan skenario yang dipilih."""
    scenario_ids = DEMO if scenario_ids is None else scenario_ids
    today = today or dt.date.today()
    rnd = random.Random(rnd_seed)
    day = lambda days_ago: (today - dt.timedelta(days=days_ago)).isoformat()

    sales = {}
    for d in range(DAYS, 0, -1):
        for s, *_ in STORES:
            for p, *_rest in PRODUCTS:
                base = _rest[-1]
                sales[(d, s, p)] = [round(base * rnd.uniform(0.8, 1.2)), 0.0]
    inventory = {(s, p): rnd.randint(80, 300) for s, *_ in STORES for p, *_ in PRODUCTS}
    deliveries = {(s, p): [DEFAULT_SUPPLIER, day(10), day(10), "DELIVERED"] for s, *_ in STORES for p, *_ in PRODUCTS}
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
            deliveries[(s, p)] = [DEFAULT_SUPPLIER, day(6), None, "DELAYED"]
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
        else:
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
                        [(s, p, stock, REORDER_POINT) for (s, p), stock in inventory.items()])
        con.executemany("INSERT INTO deliveries VALUES (?,?,?,?,?,?)", [(s, p, *v) for (s, p), v in deliveries.items()])
        con.executemany("INSERT INTO promotions VALUES (?,?,?,?,?)", promotions)
        con.executemany("INSERT INTO store_events VALUES (?,?,?,?)", events)
        con.executemany("INSERT INTO pos_sync_log VALUES (?,?,?,?)",
                        [(s, day(d), "FAILED" if (s, d) in pos_failed else "OK", pos_failed.get((s, d), ""))
                         for d in range(DAYS, 0, -1) for s, *_ in STORES])
        con.commit()
    finally:
        con.close()
    return path
