from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes.chat import router as chat_router
from app.routes.conversations import router as conversations_router
from app.routes.files import router as files_router


app = FastAPI(
    title="Philomath Backend",
    description="Backend API for the Philomath AI assistant app",
    version="1.0.0"
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def home():
    return {
        "app": "Philomath",
        "status": "backend running",
        "message": "Welcome to the Philomath backend"
    }


app.include_router(chat_router)
app.include_router(conversations_router)
app.include_router(files_router)