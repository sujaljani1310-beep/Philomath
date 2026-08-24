import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes.chat import router as chat_router
from app.routes.conversations import router as conversations_router
from app.routes.files import router as files_router
from app.routes.integrations import router as integrations_router


app = FastAPI(
    title="Philomath Backend",
    description="Backend API for the Philomath AI assistant app",
    version="2.0.0",
)


def _allowed_origins() -> list[str]:
    configured = os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    )
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
