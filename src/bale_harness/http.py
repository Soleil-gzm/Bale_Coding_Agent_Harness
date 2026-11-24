from fastapi import FastAPI, status

from bale_harness.contracts import Turn
from bale_harness.providers import create_provider
from bale_harness.settings import Settings
from bale_harness.turns import TurnRequest, build_turn


def create_app() -> FastAPI:
    create_provider(Settings())
    app = FastAPI(title="Bale Coding Agent Harness")

    @app.post(
        "/v1/turns",
        response_model=Turn,
        status_code=status.HTTP_201_CREATED,
    )
    def create_turn(request: TurnRequest) -> Turn:
        return build_turn(request)

    return app
