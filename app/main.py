from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core.errors import GatewayError, gateway_error_handler
from app.core.metrics import METRICS_ENABLED, render as render_metrics
from app.core.probe import start_probe_loop, stop_probe_loop
from app.core.redis_client import ping_redis
from app.db.bootstrap import bootstrap_admin
from app.db.seed import seed_model_prices, seed_providers
from app.db.session import init_db
from app.routers import console, openai, routes

app = FastAPI(title="LLM Gateway", version="0.0.0-m3")

# 1) API routes MUST be registered before mounting "/" (SPA catch-all would
#    otherwise swallow /v1/* and /api/* and /healthz). See M0_DEV_PLAN §2.3.
app.include_router(openai.router)
app.include_router(console.router)
app.include_router(routes.router)


@app.on_event("startup")
async def on_startup() -> None:
    # Order matters: tables -> seed providers -> bootstrap first admin (fail-loud)
    # -> Redis liveness probe (M2: quota counter requires reachable Redis).
    await init_db()
    await seed_providers()
    await seed_model_prices()
    await bootstrap_admin()
    await ping_redis()
    # M3: background quota-exhausted auto-recovery probe (no-op if no Redis).
    start_probe_loop()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    stop_probe_loop()


@app.get("/healthz")
async def healthz():
    # Liveness only — does NOT probe upstream provider reachability.
    # Readiness/dependency probes are deferred to M2 (see M0_DEV_PLAN §2.4).
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    """Prometheus exposition endpoint (M4, US-M4-04 / R7).

    Returns aggregate counters/gauges only (never VK / PII / secrets). When
    `METRICS_ENABLED=0` this returns 404 so the surface is fully absent.
    """
    if not METRICS_ENABLED:
        from fastapi import HTTPException, status as _status

        raise HTTPException(status_code=_status.HTTP_404_NOT_FOUND, detail="metrics disabled")
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(render_metrics(), media_type="text/plain; version=0.0.4")


# 2) Render GatewayError as an OpenAI-style error object (exact wire shape).
app.add_exception_handler(GatewayError, gateway_error_handler)


# 3) SPA static (last). `app/static/dist` holds the committed fallback page and,
#    after `npm run build`, the real Vite build output.
STATIC_DIR = Path(__file__).resolve().parent / "static" / "dist"
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="spa")
