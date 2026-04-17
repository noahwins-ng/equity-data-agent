import clickhouse_connect
from fastapi import FastAPI, Response
from shared.config import settings

from api.routers import reports_router

app = FastAPI(title="Equity Data Agent API")
app.include_router(reports_router)


@app.get("/health")
def health(response: Response) -> dict[str, str]:
    try:
        clickhouse_connect.get_client(
            host=settings.CLICKHOUSE_HOST,
            port=settings.CLICKHOUSE_PORT,
            connect_timeout=3,
        ).query("SELECT 1")
        ch_status = "ok"
    except Exception:
        ch_status = "unreachable"

    if ch_status != "ok":
        response.status_code = 503
        return {"status": "degraded", "clickhouse": ch_status}

    return {"status": "ok", "clickhouse": ch_status}
