import os
from contextlib import asynccontextmanager
from time import perf_counter

_import_started = perf_counter()

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.ingress import IngressMiddleware
from app.request_limits import RequestLimitsMiddleware, drain_admitted_requests

from app.observability import (
    PerformanceMiddleware,
    configure_performance_logging,
    record_timing,
    shutdown_performance_logging,
)
from app.settings import get_settings

from app.routes.chat import router as chat_router
from app.routes.conversations import router as conversations_router
from app.routes.files import router as files_router
from app.routes.integrations import router as integrations_router
from app.services.supabase_service import close_supabase_client
from app.services.startup_service import close_startup_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_started = perf_counter()
    settings = get_settings()
    configure_performance_logging(settings.performance_logging_enabled)
    record_timing("app.import", _import_started, duration_ms=_import_duration_ms)
    record_timing("app.startup", startup_started)
    try:
        yield
    finally:
        from app.providers.clients import close_provider_async_clients, close_provider_clients

        try:
            await drain_admitted_requests()
        finally:
            try:
                await run_in_threadpool(close_provider_clients)
            finally:
                try:
                    await close_provider_async_clients()
                finally:
                    try:
                        await run_in_threadpool(close_supabase_client)
                    finally:
                        try:
                            await close_startup_client()
                        finally:
                            # A slow or broken log sink must not prevent shutdown.
                            await run_in_threadpool(shutdown_performance_logging)


app = FastAPI(
    title="Philomath Backend",
    description="Backend API for the Philomath AI assistant app",
    version="2.0.0",
    lifespan=lifespan,
)


def _allowed_origins() -> list[str]:
    get_settings()  # Load supported dotenv configuration before reading CORS.
    configured = os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    )
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


app.add_middleware(IngressMiddleware)
app.add_middleware(RequestLimitsMiddleware)
# CORS also applies to early resource/deadline errors and preflight requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(PerformanceMiddleware)


@app.exception_handler(RequestValidationError)
async def sanitized_validation_error(request, error):
    # Pydantic's default input/ctx fields may echo a BYOK key or document text.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
        for item in error.errors()
    ]})


@app.get("/")
def home():
    return {
        "app": "Philomath",
        "status": "backend running",
        "message": "Welcome to the Philomath backend",
    }


app.include_router(chat_router)
app.include_router(conversations_router)
app.include_router(files_router)
app.include_router(integrations_router)

_import_duration_ms = round((perf_counter() - _import_started) * 1000, 3)
