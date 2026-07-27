"""
Local OpenAI-compatible embedding server for the LLM Gateway semantic cache.

Serves BAAI/bge-small-zh-v1.5 (or any sentence-transformers model) behind an
OpenAI-style `/v1/embeddings` endpoint so the gateway can call it via
`litellm.aembedding(model="openai/<model>", api_base=<this server>/v1)`.

Why a custom server instead of Ollama/vLLM: we fully control the response
contract (OpenAI embeddings spec) and bake the model at build time, so the
container is ready on first boot with no extra pull step.

Run (dev, no docker):
    pip install -r embed_requirements.txt
    EMBED_MODEL=BAAI/bge-small-zh-v1.5 EMBED_SERVED_NAME=bge-small-zh-v1.5 \
        uvicorn server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import List, Union

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel

EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
EMBED_SERVED_NAME = os.getenv("EMBED_SERVED_NAME", "bge-small-zh-v1.5")

_model = None


def get_model():
    """Lazily load (and cache) the sentence-transformers model."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(EMBED_MODEL)
    return _model


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-load on startup so the first request isn't slow / the healthcheck
    # only turns green once the model is actually usable.
    get_model()
    yield


app = FastAPI(title="bge embedding server (OpenAI-compatible)", lifespan=lifespan)


class EmbedRequest(BaseModel):
    model: str = EMBED_SERVED_NAME
    input: Union[str, List[str]]
    encoding_format: str = "float"  # accepted but ignored (always float32)


@app.get("/health")
def health():
    return {"status": "ok", "model": EMBED_SERVED_NAME}


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": EMBED_SERVED_NAME,
                "object": "model",
                "owned_by": "local",
                "root": EMBED_SERVED_NAME,
                "parent": None,
                "permission": [],
            }
        ],
    }


@app.post("/v1/embeddings")
def embeddings(req: EmbedRequest):
    model = get_model()
    texts = req.input if isinstance(req.input, list) else [req.input]

    # bge recommends a retrieval query prefix, but for a symmetric cache
    # (query vs stored query) we skip it so both sides share one embedding
    # space and the cosine comparison stays consistent.
    vecs = model.encode(
        texts,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    vecs = np.atleast_2d(np.asarray(vecs, dtype="float32"))

    data = []
    for i, vec in enumerate(vecs):
        data.append(
            {
                "object": "embedding",
                "index": i,
                "embedding": vec.tolist(),
            }
        )

    # Rough token estimate for usage accounting (no tokenizer needed here).
    n_tokens = sum(max(1, len(t.split())) for t in texts)
    return {
        "object": "list",
        "data": data,
        "model": req.model,
        "usage": {"prompt_tokens": n_tokens, "total_tokens": n_tokens},
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
