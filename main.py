import os
import time
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from config import APP_START_TIME, vector_store
from routes.auth import router as auth_router
from routes.chat import router as chat_router
from routes.ingest import router as ingest_router
from schemas import HealthResponse

app = FastAPI(
    title="Orderly — Customer Support API",
    description="API for customer order support, policy RAG, and automated ticket handling.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(ingest_router)


@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check():
    return HealthResponse(
        status="healthy",
        uptime_seconds=round(time.time() - APP_START_TIME, 2),
        vector_store_connected=vector_store is not None,
    )


@app.get("/")
async def get_index():
    index_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Orderly API is running. UI not found in static/index.html"}


static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


