from fastapi import FastAPI

app = FastAPI(title="Equity Data Agent API")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
