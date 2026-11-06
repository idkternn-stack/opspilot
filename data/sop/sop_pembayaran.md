# SOP Masalah Pembayaran Pelanggan
1. Pantau status transaksi pembayaran (SUCCESS, FAILED, DECLINED) dan kolom charged/refunded setiap hari.
2. Transaksi berstatus DECLINED (ditolak) tetapi charged=1 berarti dana pelanggan tetap ditagih padahal transaksi ditolak. Refund wajib diproses paling lambat 1x24 jam ke metode bayar asal.
3. Buat tiket HIGH ke Finance & Payment Operations untuk rekonsiliasi dengan bank atau payment gateway, membatalkan penagihan yang tidak sah, dan memperbaiki penyebab di sistem.
4. Kirim notifikasi ke pelanggan terdampak berisi penjelasan dan estimasi waktu refund.
5. Transaksi FAILED dengan charged=0 tidak memotong dana pelanggan, jadi tidak perlu refund. Jika tingkat gagal bayar lebih dari 3x normal, buat tiket HIGH ke IT Payment dan arahkan pelanggan ke metode bayar lain sementara.
