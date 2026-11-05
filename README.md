# OpsPilot

**AI agent otonom untuk investigasi anomali operasional retail.** GTNIC 2026, sub tema Enterprise Productivity.

OpsPilot memantau data toko secara berkala. Begitu menemukan anomali, OpsPilot mengumpulkan bukti lewat tool (SQL read-only, estimasi kerugian, RAG atas SOP), lalu AI (**Gemini**, dengan **Groq** sebagai cadangan opsional) menalar akar masalah dan mengusulkan tindakan yang benar-benar mengubah data (transfer stok, hentikan promo, tiket, notifikasi). Setiap angka dalam kesimpulan AI diverifikasi kode terhadap bukti. Desainnya hemat kuota: 1-2 panggilan AI per investigasi, sehingga tetap jalan di API gratis. Tidak ada tindakan yang dijalankan tanpa persetujuan manusia.

## Fitur utama

| Fitur | Penjelasan |
|---|---|
| Autopilot | Monitor latar belakang mendeteksi anomali tiap 2 menit dan langsung mengantrekan investigasi, tanpa perlu diklik |
| Investigasi hemat kuota | Bukti dikumpulkan otomatis lewat tool (SQL, estimasi kerugian, RAG SOP); AI menalar dalam 1-2 panggilan dan boleh meminta query tambahan |
| Verifikasi berbasis kode | Setiap angka di klaim AI dicocokkan dengan bukti yang dirujuk; bila tidak cocok, AI diminta memperbaiki satu kali |
| Multi-penyedia AI | Gemini (utama) dan Groq (cadangan opsional), pembatas permintaan per menit, pindah otomatis saat kuota habis |
| Mode tanpa AI | Bila semua model AI habis kuota, diagnosis berbasis aturan tetap berjalan dengan label jelas |
| Aksi nyata dengan persetujuan | `transfer_stock` dan `pause_promo` mengubah database setelah disetujui, lalu hasilnya diverifikasi |
| Progres langsung | Setiap langkah agent tampil di UI saat terjadi |
| Jejak audit | Semua investigasi, langkah, usulan, dan keputusan tersimpan di database terpisah |
| Mode rekaman | Bila Gemini sedang sibuk saat demo, investigasi sebelumnya bisa diputar ulang dengan label jelas "rekaman" |
| Riset perusahaan nyata | Ketik nama perusahaan apa pun; OpsPilot mencari masalah operasional terbarunya lewat Google Search, memverifikasi sumber, menilai dampak, dan merujuk SOP |
| Simulasi kasus nyata | Temuan riset bisa disimulasikan di data toko demo, lalu diinvestigasi agent seperti anomali biasa |
| Evaluasi otomatis | 8 skenario dengan akar masalah diketahui; akurasi diagnosis, ketepatan aksi, keamanan, dan waktu diukur otomatis |

## Arsitektur

```mermaid
flowchart LR
  M[Monitor autopilot] --> D[Detektor anomali]
  U[Pengguna] --> UI[Web UI]
  UI <--> API[FastAPI]
  D --> Q[Antrean investigasi]
  API --> Q
  Q --> EV[Pengumpul bukti otomatis<br/>SQL, estimasi, RAG SOP]
  EV --> A[Penalaran AI 1-2 panggilan<br/>Gemini, cadangan Groq]
  A --> VC[Verifikasi kode<br/>angka vs bukti]
  A -->|run_sql_readonly| DB[(Data toko<br/>SQLite read-only)]
  A -->|search_sop| R[RAG TF-IDF atas SOP]
  A -->|estimate_loss| DB
  A -->|usulan aksi| V{Validasi}
  V --> H{Persetujuan manusia}
  H -->|disetujui| X[transfer_stock, pause_promo,<br/>tiket, notifikasi]
  X --> DB
  A --> L[(Jejak audit<br/>app.db)]
  L --> UI
```

| Komponen | Teknologi |
|---|---|
| LLM | Gemini (Google GenAI SDK) + Groq opsional (OpenAI-compatible API), keluaran JSON terstruktur, pembatas per menit, fallback otomatis |
| Backend | Python, FastAPI, thread worker dan monitor |
| Data | SQLite: data toko sintetis 30 hari (4 toko, 8 produk) + database audit terpisah |
| RAG | TF-IDF (scikit-learn) atas 6 dokumen SOP di `data/sop` |
| Frontend | HTML + JavaScript tanpa framework, polling progres tiap detik |

## Riset perusahaan nyata

1. **Cari:** Gemini dengan Grounding with Google Search menulis temuan dalam format tetap (judul, tanggal, kategori, ringkasan).
2. **Verifikasi (kode, bukan LLM):** setiap temuan dipetakan ke sumbernya lewat metadata grounding. Temuan tanpa sumber dibuang. Temuan yang sumbernya hanya media sosial ditandai "belum terkonfirmasi". Tanggal lebih dari 180 hari atau di masa depan ditandai.
3. **Analisis:** dampak operasional, tingkat risiko, mitigasi sesuai SOP (RAG), dan jenis skenario yang paling mirip.
4. **Simulasi:** pola masalahnya diterapkan ke data toko demo. Injeksi dicoba dulu di salinan database dan baru diterapkan bila detektor benar-benar menghasilkan anomali baru. Anomali diberi label "simulasi kasus <perusahaan>" dan langsung diinvestigasi.

Temuan selalu berasal dari sumber publik, bukan data internal perusahaan. Nama perusahaan dari pengguna dibersihkan dan dibatasi 80 karakter sebelum dipakai di prompt.

## Skenario anomali

| ID | Kasus | Tindakan yang diharapkan |
|---|---|---|
| `stockout_bandung_minyak` | Stok habis karena pengiriman terlambat | Transfer stok |
| `stockout_medan_susu` | Stok habis karena pengiriman terlambat | Transfer stok |
| `discount_surabaya_beras` | Diskon 40% di bawah harga pokok | Hentikan promo |
| `discount_jakarta_kopi` | Diskon 50% di bawah harga pokok | Hentikan promo |
| `supplier_gula_multi_toko` | Satu supplier terlambat ke dua toko | Tiket ke Procurement |
| `lonjakan_medan_mie` | Permintaan melonjak, stok menipis | Transfer stok atau tiket |
| `data_pos_jakarta` | Data hilang karena sinkronisasi POS gagal | Tiket IT, tanpa aksi stok/harga |
| `toko_tutup_surabaya` | Pendapatan turun karena renovasi terjadwal | Tidak perlu tindakan korektif |

Data toko demo dimulai **bersih tanpa anomali**. Alur demo: riset perusahaan nyata, simulasikan temuannya, lalu agent menyelesaikannya. Skenario di tabel ini dipakai evaluasi (dijalankan satu per satu) dan tes offline.

## Guardrails dan mitigasi risiko

- SQL hanya `SELECT`, satu statement, koneksi read-only, maksimal 50 baris.
- Database audit terpisah dari data toko, jadi agent tidak bisa membaca atau mengubah jejak audit.
- Usulan aksi divalidasi saat diusulkan dan saat disetujui (stok cukup, toko asal tidak di bawah reorder point, promo memang aktif).
- Usulan ganda otomatis digabung.
- Semua aksi menunggu persetujuan manusia; hasil eksekusi dicek ulang dari database.
- Agent hanya boleh memakai fakta dari hasil tool, dan wajib lolos verifikasi diri.
- Kuota AI: 1-2 panggilan per investigasi, pembatas permintaan per menit, cache riset 24 jam, pindah ke model/penyedia cadangan, lalu mode tanpa AI berlabel atau rekaman.
- API key disimpan di `.env` dan tidak masuk repository.

## Menjalankan secara lokal (Windows)

1. Install Python 3.11 atau lebih baru.
2. Buat file `.env` (lihat `.env.example`):
   ```
   GEMINI_API_KEY=key_kamu
   GEMINI_FALLBACK_MODEL=gemini-3.5-flash
   GROQ_API_KEY=key_groq_kamu   (opsional, cadangan bila kuota Gemini habis)
   ```
   Cek key dengan `cek_key.bat`.
3. Klik dua kali `start.bat`. Browser terbuka otomatis ke http://127.0.0.1:7860.
4. Ketik nama perusahaan di **Riset perusahaan nyata**, lalu klik **Simulasikan di OpsPilot** pada temuan yang ingin diselesaikan.

Cara manual:
```bash
pip install -r requirements.txt
python -m uvicorn backend.main:app --port 7860
```

## Evaluasi dan tes

```bash
python -m backend.evaluate      # atau klik evaluate.bat; butuh API key
python -m tests.test_offline    # atau klik test.bat; tanpa API key
```

Hasil evaluasi tersimpan di `eval/results/` dalam format Markdown dan JSON.

## API

| Method | Endpoint | Fungsi |
|---|---|---|
| GET | `/api/state` | Status autopilot, daftar anomali, riwayat |
| POST | `/api/autopilot` | Aktifkan atau nonaktifkan autopilot |
| POST | `/api/check` | Deteksi anomali sekarang |
| POST | `/api/investigate` | Antrekan investigasi langsung |
| POST | `/api/replay` | Putar rekaman investigasi |
| GET | `/api/runs/{id}?after=n` | Detail investigasi dan langkah baru |
| POST | `/api/decide` | Setujui atau tolak usulan |
| POST | `/api/research` | Mulai riset perusahaan `{"company": "..."}` |
| GET | `/api/research/{id}?after=n` | Hasil riset dan langkah baru |
| POST | `/api/research/{id}/simulate` | Simulasikan satu temuan `{"issue_id": 1}` |
| POST | `/api/reset` | Kembalikan data demo ke kondisi awal |

## Struktur

```
backend/
  llm.py        Gemini + Groq, pembatas kecepatan, fallback
  evidence.py   pengumpulan bukti otomatis per jenis anomali
  agent.py      penalaran AI + verifikasi berbasis kode
  rules.py      diagnosis berbasis aturan (mode tanpa AI)
  tools.py      tool baca, tool aksi, validasi
  detect.py     detektor anomali
  scenarios.py  data sintetis dan skenario
  research.py   riset perusahaan lewat Google Search dan verifikasi sumber
  simulate.py   simulasi pola kasus nyata ke data demo
  engine.py     autopilot, antrean, riset, mode rekaman
  store.py      jejak audit dan rekaman
  main.py       API
  evaluate.py   evaluasi otomatis
frontend/index.html
data/sop/       dokumen SOP untuk RAG
data/recordings/ rekaman investigasi untuk mode cadangan
tests/          tes offline dengan Gemini tiruan
```

## Deploy

Hugging Face Spaces (SDK Docker, port 7860) atau Render. Isi `GEMINI_API_KEY` di Secrets/Environment, bukan di kode.
