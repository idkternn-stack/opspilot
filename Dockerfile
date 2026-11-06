FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Hosting seperti Hugging Face Spaces menjalankan container sebagai user non-root.
RUN mkdir -p data/recordings && chmod -R 777 data
# Render/Railway mengisi PORT sendiri; Hugging Face Spaces memakai 7860.
ENV PORT=7860
EXPOSE 7860
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
