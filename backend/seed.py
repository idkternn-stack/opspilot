"""Membuat database SQLite berisi data retail sintetis 30 hari.
Dua anomali disisipkan dengan sengaja:
  1. Stockout Minyak Goreng di Bandung Dago karena pengiriman supplier terlambat.
  2. Diskon 40% yang salah pada Beras 5kg di Surabaya Tunjungan (harga jual di bawah harga pokok).
"""
import datetime as dt
import os
import random
import sqlite3

from .config import DB_PATH

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

SCHEMA = """
CREATE TABLE stores(id INTEGER PRIMARY KEY, name TEXT, city TEXT);
CREATE TABLE products(id INTEGER PRIMARY KEY, name TEXT, category TEXT, price REAL, cost REAL, base_qty INTEGER);
CREATE TABLE sales(date TEXT, store_id INT, product_id INT, qty INT, discount REAL);
CREATE TABLE inventory(store_id INT, product_id INT, stock INT, reorder_point INT);
CREATE TABLE deliveries(store_id INT, product_id INT, supplier TEXT, expected_date TEXT, received_date TEXT, status TEXT);
CREATE TABLE tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, priority TEXT, description TEXT, created_at TEXT);
CREATE TABLE notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT, message TEXT, created_at TEXT);
"""


def seed():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    random.seed(7)
    today = dt.date.today()
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO stores VALUES (?,?,?)", STORES)
    con.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", PRODUCTS)

    for days_ago in range(30, 0, -1):
        day = (today - dt.timedelta(days=days_ago)).isoformat()
        for store_id, *_ in STORES:
            for product_id, _, _, _, _, base in PRODUCTS:
                qty, discount = round(base * random.uniform(0.8, 1.2)), 0.0
                if store_id == 2 and product_id == 1 and days_ago <= 4:
                    qty = 0  # anomali 1: stok habis
                if store_id == 3 and product_id == 2 and days_ago <= 3:
                    discount = 0.4  # anomali 2: diskon salah
                con.execute("INSERT INTO sales VALUES (?,?,?,?,?)", (day, store_id, product_id, qty, discount))

    delivered = (today - dt.timedelta(days=10)).isoformat()
    for store_id, *_ in STORES:
        for product_id, *_ in PRODUCTS:
            stock = random.randint(80, 300)
            if (store_id, product_id) == (2, 1):
                stock = 0
            if (store_id, product_id) == (1, 1):
                stock = 300  # Jakarta punya stok berlebih untuk transfer
            con.execute("INSERT INTO inventory VALUES (?,?,?,50)", (store_id, product_id, stock))
            con.execute("INSERT INTO deliveries VALUES (?,?,?,?,?,?)",
                        (store_id, product_id, "PT Sumber Pangan", delivered, delivered, "DELIVERED"))
    con.execute("UPDATE deliveries SET expected_date=?, received_date=NULL, status='DELAYED' "
                "WHERE store_id=2 AND product_id=1", ((today - dt.timedelta(days=6)).isoformat(),))
    con.commit()
    con.close()


if __name__ == "__main__":
    seed()
    print("Database dibuat:", DB_PATH)
