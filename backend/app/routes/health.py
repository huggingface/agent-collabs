from fastapi import APIRouter

router = APIRouter()


@router.get("/v1/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
