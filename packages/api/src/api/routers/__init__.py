from api.routers.data import router as data_router
from api.routers.reports import router as reports_router
from api.routers.search import router as search_router
from api.routers.tickers import router as tickers_router

__all__ = ["data_router", "reports_router", "search_router", "tickers_router"]
