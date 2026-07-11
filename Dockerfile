FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Runtime ─────────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# Copy Python packages from builder
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application
COPY collector.py analyzer.py main.py /app/

# Volume for persistent history
VOLUME /data

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import urllib.request; r = urllib.request.urlopen('http://localhost:9462/health'); assert r.status == 200"

EXPOSE 9462
CMD ["python3", "main.py"]