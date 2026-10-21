"""Membuat data sintetis retail (SQLite) + dokumen SOP. Anomali disisipkan sengaja."""
import sqlite3, random, os, datetime as dt
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "opspilot.db")
SOP = os.path.join(ROOT, "data", "sop")
SOPS = {
 "sop_stockout.md": "# SOP Stockout dan Keterlambatan Pengiriman\n1. Jika produk top-seller habis lebih dari 24 jam, buat tiket prioritas HIGH ke Supply Chain.\n2. Jika pengiriman supplier terlambat lebih dari 3 hari, hubungi supplier alternatif.\n3. Lakukan transfer stok antar toko bila toko lain memiliki stok lebih dari 2x reorder point.\n4. Beritahu Store Manager dan Regional Manager.",
 "sop_pricing.md": "# SOP Kesalahan Harga dan Diskon\n1. Diskon di atas 25% wajib punya persetujuan Pricing Manager.\n2. Jika harga jual setelah diskon di bawah harga pokok (cost), hentikan promo segera dan buat tiket prioritas HIGH ke Pricing Team.\n3. Hitung estimasi kerugian per hari dan laporkan ke Finance.",
 "sop_eskalasi.md": "# SOP Eskalasi Anomali Penjualan\n1. Penurunan pendapatan harian lebih dari 20% selama 3 hari berturut-turut wajib diinvestigasi.\n2. Cek stok, pengiriman, dan diskon sebelum menyimpulkan penyebab.\n3. Prioritas tiket: HIGH bila dampak lebih dari Rp1 juta per hari, selain itu MEDIUM.",
}
STORES = [(1, "Jakarta Pusat", "Jakarta"), (2, "Bandung Dago", "Bandung"), (3, "Surabaya Tunjungan", "Surabaya"), (4, "Medan Kota", "Medan")]
PRODUCTS = [(1, "Minyak Goreng 2L", "Sembako", 38000, 31000, 40), (2, "Beras 5kg", "Sembako", 68000, 60000, 25),
 (3, "Gula 1kg", "Sembako", 16000, 13500, 30), (4, "Kopi Sachet Box", "Minuman", 12000, 8500, 35),
 (5, "Susu UHT 1L", "Minuman", 20000, 16000, 30), (6, "Mie Instan", "Makanan", 3500, 2800, 80),
 (7, "Sabun Mandi", "Perawatan", 5000, 3500, 40), (8, "Teh Botol", "Minuman", 6000, 4200, 50)]

def seed():
    os.makedirs(SOP, exist_ok=True)
    for f, t in SOPS.items(): open(os.path.join(SOP, f), "w", encoding="utf-8").write(t)
    if os.path.exists(DB): os.remove(DB)
    c = sqlite3.connect(DB); random.seed(7)
    c.executescript("""CREATE TABLE stores(id INTEGER PRIMARY KEY,name TEXT,city TEXT);
CREATE TABLE products(id INTEGER PRIMARY KEY,name TEXT,category TEXT,price REAL,cost REAL,base_qty INTEGER);
CREATE TABLE sales(date TEXT,store_id INT,product_id INT,qty INT,discount REAL);
CREATE TABLE inventory(store_id INT,product_id INT,stock INT,reorder_point INT);
CREATE TABLE deliveries(store_id INT,product_id INT,supplier TEXT,expected_date TEXT,received_date TEXT,status TEXT);
CREATE TABLE tickets(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT,priority TEXT,description TEXT,created_at TEXT);
CREATE TABLE notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,recipient TEXT,message TEXT,created_at TEXT);""")
    c.executemany("INSERT INTO stores VALUES(?,?,?)", STORES)
    c.executemany("INSERT INTO products VALUES(?,?,?,?,?,?)", PRODUCTS)
    today = dt.date.today()
    for d in range(30, 0, -1):
        day = (today - dt.timedelta(days=d)).isoformat()
        for s, *_ in STORES:
            for p, _, _, _, _, base in PRODUCTS:
                qty, disc = round(base * random.uniform(0.8, 1.2)), 0.0
                if s == 2 and p == 1 and d <= 4: qty = 0          # stockout Bandung
                if s == 3 and p == 2 and d <= 3: disc = 0.4       # salah diskon Surabaya
                c.execute("INSERT INTO sales VALUES(?,?,?,?,?)", (day, s, p, qty, disc))
    for s, *_ in STORES:
        for p, *_ in PRODUCTS:
            stock = random.randint(80, 300)
            if s == 2 and p == 1: stock = 0
            if s == 1 and p == 1: stock = 300
            c.execute("INSERT INTO inventory VALUES(?,?,?,50)", (s, p, stock))
            c.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?)", (s, p, "PT Sumber Pangan", (today - dt.timedelta(10)).isoformat(), (today - dt.timedelta(10)).isoformat(), "DELIVERED"))
    c.execute("UPDATE deliveries SET expected_date=?,received_date=NULL,status='DELAYED' WHERE store_id=2 AND product_id=1", ((today - dt.timedelta(6)).isoformat(),))
    c.commit(); c.close()

if __name__ == "__main__": seed(); print("Seed selesai:", DB)
