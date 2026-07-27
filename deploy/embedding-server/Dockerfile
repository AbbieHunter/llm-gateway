FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install a CPU-only torch first (the pytorch CPU index is the PRIMARY source
# for this step) so the much larger CUDA build is never pulled in.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch

COPY embed_requirements.txt .
RUN pip install --no-cache-dir -r embed_requirements.txt

COPY server.py .

# Pre-download the embedding model at build time so the container is ready on
# first boot (no runtime pull, no extra init container).
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5')"

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
