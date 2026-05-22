FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir pillow watchdog

COPY . .

ENV GALLERY_DIR=/app
ENV TRABAJOS_DIR=/trabajos
ENV PORT=8765

EXPOSE 8765

CMD ["python", "server.py"]
