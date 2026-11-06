# Dockerfile untuk Hugging Face Spaces (SDK Docker).
# Salin isi file ini ke file "Dockerfile" di Space. Kode diambil langsung dari GitHub,
# dan baris ADD membuat Space otomatis memakai commit terbaru setiap kali di-rebuild.
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ADD https://api.github.com/repos/idkternn-stack/opspilot/commits/main /tmp/latest-commit.json
RUN git clone --depth 1 https://github.com/idkternn-stack/opspilot.git . && rm -rf .git
RUN pip install --no-cache-dir -r requirements.txt
# Spaces menjalankan container sebagai user non-root: folder data harus bisa ditulis.
RUN mkdir -p data/recordings && chmod -R 777 data
ENV PORT=7860
EXPOSE 7860
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
