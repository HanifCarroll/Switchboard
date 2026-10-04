"""FastAPI application assembly."""

from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from switchboard.api.investigations import router as investigations_router
from switchboard.api.proposals import router as proposals_router
from switchboard.api.workspace import router as workspace_router
from switchboard.auth import get_auth_mode
from switchboard.investigation.mode import get_investigation_mode
from switchboard.storage import StorageError


@asynccontextmanager
async def lifespan(_app: FastAPI):
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    get_investigation_mode()
    get_auth_mode()
    yield


app = FastAPI(title="Switchboard demo", lifespan=lifespan)


@app.exception_handler(StorageError)
def storage_error_handler(_request: Request, error: StorageError) -> JSONResponse:
    status = error.status if 400 <= error.status < 500 else 503
    return JSONResponse(status_code=status, content={"detail": str(error)})


app.include_router(workspace_router)
app.include_router(proposals_router)
app.include_router(investigations_router)
