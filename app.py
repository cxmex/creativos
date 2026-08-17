import os
import asyncio
from datetime import datetime, timedelta
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()
templates = Jinja2Templates(directory="templates")

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# Simple TTL cache
_cache: dict = {}
CACHE_TTL = 300  # seconds


def cache_get(key):
    if key in _cache:
        data, ts = _cache[key]
        if (datetime.utcnow() - ts).total_seconds() < CACHE_TTL:
            return data
    return None


def cache_set(key, data):
    _cache[key] = (data, datetime.utcnow())


async def sb(client, method, endpoint, params=None, json_data=None):
    url = f"{SUPABASE_URL}{endpoint}"
    r = await client.request(method.upper(), url, headers=HEADERS,
                             params=params, json=json_data, timeout=30)
    return r.json() if r.content else None


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/api/thumbs")
async def api_thumbs():
    cached = cache_get("thumbs")
    if cached is not None:
        return cached

    async with httpx.AsyncClient() as client:
        rows = await sb(client, "POST", "/storage/v1/object/list/images_estilos",
                        json_data={"prefix": "", "limit": 5000, "offset": 0})
    thumbs = {}
    for r in (rows or []):
        name = r.get("name", "")
        if "/" in name:
            estilo_id = name.split("/")[0]
            if estilo_id not in thumbs:
                thumbs[estilo_id] = f"{SUPABASE_URL}/storage/v1/object/public/images_estilos/{name}"

    cache_set("thumbs", thumbs)
    return thumbs


@app.get("/api/estilos")
async def api_estilos(sort: str = "nombre"):
    cached = cache_get("estilos")
    if cached is None:
        async with httpx.AsyncClient() as client:
            result = await sb(client, "POST", "/rest/v1/rpc/get_creativos_estilos", json_data={})
        cached = result or []
        cache_set("estilos", cached)

    rows = list(cached)

    if sort == "stock_total":
        rows.sort(key=lambda x: x["total"], reverse=True)
    elif sort == "stock_t1":
        rows.sort(key=lambda x: x["t1"], reverse=True)
    elif sort == "stock_t2":
        rows.sort(key=lambda x: x["t2"], reverse=True)
    elif sort == "colores":
        rows.sort(key=lambda x: x["num_colors"], reverse=True)
    else:
        rows.sort(key=lambda x: x["nombre"])

    return rows


@app.get("/api/ventas-por-estilo")
async def api_ventas_por_estilo(dias: int = 30):
    key = f"ventas_{dias}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    async with httpx.AsyncClient() as client:
        result = await sb(client, "POST", "/rest/v1/rpc/get_ventas_por_estilo",
                          json_data={"dias": dias})
    data = result or {}
    cache_set(key, data)
    return data


@app.post("/api/cache/clear")
async def clear_cache():
    _cache.clear()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
