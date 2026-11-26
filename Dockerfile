FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Small runtime libraries used by OpenCV / ONNX Runtime (same list as packages.txt)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY core core
COPY agent agent
COPY mcp_server mcp_server
COPY samples samples
COPY streamlit_app.py .

# Download the PP-OCRv5 mobile models at build time so start-up needs no network
RUN python -c "from core.ocr import _engine; _engine()"

# Run as a non-root user
RUN useradd -m -u 1000 user \
    && mkdir -p /app/data /app/uploads /app/traces \
    && chown -R user /app/data /app/uploads /app/traces
USER user

# API keys are NOT baked in: pass GROQ_API_KEY / GEMINI_API_KEY at run time (--env-file .env)
ENV DB_PATH=/app/data/app.db \
    UPLOAD_DIR=/app/uploads \
    TRACE_FILE=/app/traces/trace.jsonl \
    DEFAULT_CURRENCY=MYR
EXPOSE 8501
CMD ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
