from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import config, health, query, sessions

load_dotenv()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Claude Code HTTP Service",
        description="FastAPI + SSE wrapper for Claude Agent SDK.",
        version=os.getenv("APP_VERSION", "0.1.0"),
    )

    origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",")]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(config.router, prefix="/v1", tags=["configuration"])
    app.include_router(query.router, prefix="/v1", tags=["query"])
    app.include_router(sessions.router, prefix="/v1", tags=["sessions"])
    return app


app = create_app()
