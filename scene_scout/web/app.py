"""
SceneScout web application — onboarding and profile viewer.

FastAPI serves JSON API routes and static HTML/CSS/JS for the noir supper club UI.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from scene_scout.agents import user_preference
from scene_scout.agents.user_preference import UserProfileNotFoundError
from scene_scout.services.llm import LLMInfrastructureError, LLMValidationError

_STATIC_DIR = Path(__file__).parent / "static"
_BASIC_REALM = "SceneScout"
_BASIC_USERNAME = "scenescout"
_ONBOARDING_RUN_ID_PREFIX = "web-onboarding"
_security = HTTPBasic(auto_error=False)


class OnboardingRequest(BaseModel):
    """JSON body for cold-start onboarding."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    email: str = Field(min_length=1)
    prompt: str = Field(min_length=1)


def _web_password() -> str | None:
    password = os.getenv("WEB_PASSWORD")
    if password:
        return password
    return None


def _onboarding_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{_ONBOARDING_RUN_ID_PREFIX}-{stamp}"


def validate_onboarding_inputs(name: str, email: str, prompt: str) -> str | None:
    """Return a client-facing validation error message, or None if valid."""
    if not name.strip():
        return "Name is required."
    if not email.strip():
        return "Email is required."
    if "@" not in email:
        return "Email must look like a valid address."
    if not prompt.strip():
        return "Your taste is required."
    return None


def _credentials_valid(credentials: HTTPBasicCredentials | None) -> bool:
    expected = _web_password()
    if expected is None:
        return True
    if credentials is None:
        return False
    username_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        _BASIC_USERNAME.encode("utf-8"),
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        expected.encode("utf-8"),
    )
    return username_ok and password_ok


def _unauthorized_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": "Authentication required."},
        headers={"WWW-Authenticate": f'Basic realm="{_BASIC_REALM}"'},
    )


async def disable_frontend_cache(request: Request, call_next):
    """Prevent browsers from serving stale HTML/CSS/JS during local development."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".css", ".js")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


async def require_basic_auth(request: Request, call_next):
    """Enforce HTTP Basic auth when ``WEB_PASSWORD`` is set."""
    if request.url.path == "/health":
        return await call_next(request)
    if _web_password() is None:
        return await call_next(request)

    credentials = await _security(request)
    if not _credentials_valid(credentials):
        return _unauthorized_response()
    return await call_next(request)


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    application = FastAPI(title="SceneScout", version="0.1.0")
    application.middleware("http")(disable_frontend_cache)
    application.middleware("http")(require_basic_auth)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/api/onboarding", response_model=None)
    async def onboarding(body: OnboardingRequest):
        validation_error = validate_onboarding_inputs(
            body.name,
            body.email,
            body.prompt,
        )
        if validation_error:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": validation_error},
            )

        try:
            profile = await user_preference.parse_cold_start(
                name=body.name,
                email=body.email,
                prompt=body.prompt,
                run_id=_onboarding_run_id(),
            )
        except LLMInfrastructureError as exc:
            return JSONResponse(
                status_code=status.HTTP_502_BAD_GATEWAY,
                content={"error": str(exc)},
            )
        except LLMValidationError as exc:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"error": str(exc)},
            )

        return profile

    @application.get("/api/profile", response_model=None)
    async def profile():
        try:
            return user_preference.load_profile()
        except UserProfileNotFoundError:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": "No profile saved yet."},
            )

    application.mount(
        "/",
        StaticFiles(directory=_STATIC_DIR, html=True),
        name="static",
    )
    return application


app = create_app()


def main() -> None:
    """Launch uvicorn for local development and Docker."""
    port = int(os.getenv("WEB_SERVER_PORT", "7860"))
    uvicorn.run(
        "scene_scout.web.app:app",
        host="0.0.0.0",
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
