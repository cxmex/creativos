import os
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict
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

_cache: dict = {}
CACHE_TTL = 300


def cache_get(key):
    if key in _cache:
        data, ts = _cache[key]
        if (datetime.utcnow() - ts).total_seconds() < CACHE_TTL:
            return data
    return None


def cache_set(key, data):
    _cache[key] = (data, datetime.utcnow())


async def sb(client, method, endpoint, params=None, json_data=None, extra_headers=None):
    url = f"{SUPABASE_URL}{endpoint}"
    hdrs = {**HEADERS, **(extra_headers or {})}
    r = await client.request(method.upper(), url, headers=hdrs,
                             params=params, json=json_data, timeout=30)
    return r.json() if r.content else None


async def _fetch_all_inventario1(client):
    """Fetch all inventario1 rows with concurrent pagination."""
    page_size = 1000

    # Get total count first
    count_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inventario1",
        headers={**HEADERS, "Prefer": "count=exact"},
        params={"select": "id", "limit": "1"},
        timeout=15,
    )
    content_range = count_resp.headers.get("content-range", "0-0/0")
    total = int(content_range.split("/")[-1]) if "/" in content_range else 0

    if total == 0:
        return []

    # Build page offsets
    offsets = list(range(0, total, page_size))

    async def fetch_page(offset):
        return await sb(client, "GET", "/rest/v1/inventario1",
                        params={"select": "estilo_id,color,terex1,terex2",
                                "limit": str(page_size), "offset": str(offset)})

    pages = await asyncio.gather(*[fetch_page(o) for o in offsets])
    all_rows = []
    for page in pages:
        if page:
            all_rows.extend(page)
    return all_rows


async def _build_estilos_from_rest():
    async with httpx.AsyncClient() as client:
        estilos, stock_rows = await asyncio.gather(
            sb(client, "GET", "/rest/v1/inventario_estilos",
               params={"select": "id,nombre,proveedor", "limit": "1000"}),
            _fetch_all_inventario1(client),
        )

    agg = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in (stock_rows or []):
        eid = r.get("estilo_id")
        if not eid:
            continue
        color = (r.get("color") or "SIN COLOR").strip().upper()
        agg[eid][color][0] += r.get("terex1") or 0
        agg[eid][color][1] += r.get("terex2") or 0

    result = []
    for e in (estilos or []):
        eid = e["id"]
        colors = [
            {"color": c, "t1": v[0], "t2": v[1], "total": v[0] + v[1]}
            for c, v in agg.get(eid, {}).items()
        ]
        colors.sort(key=lambda x: x["total"], reverse=True)
        t1 = sum(c["t1"] for c in colors)
        t2 = sum(c["t2"] for c in colors)
        result.append({
            "id": eid,
            "nombre": e["nombre"],
            "proveedor": e.get("proveedor") or "",
            "colors": colors,
            "t1": t1,
            "t2": t2,
            "total": t1 + t2,
            "num_colors": len(colors),
        })
    return result


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

        # RPC not available yet — fall back to paginated REST (fast: concurrent pages)
        if not isinstance(result, list):
            result = await _build_estilos_from_rest()

        cached = result or []
        cache_set("estilos", cached)

    rows = list(cached)

    if sort == "stock_total":
        rows.sort(key=lambda x: x.get("total", 0), reverse=True)
    elif sort == "stock_t1":
        rows.sort(key=lambda x: x.get("t1", 0), reverse=True)
    elif sort == "stock_t2":
        rows.sort(key=lambda x: x.get("t2", 0), reverse=True)
    elif sort == "colores":
        rows.sort(key=lambda x: x.get("num_colors", 0), reverse=True)
    else:
        rows.sort(key=lambda x: x.get("nombre", ""))

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
    data = result if isinstance(result, dict) else {}
    cache_set(key, data)
    return data


@app.post("/api/cache/clear")
async def clear_cache():
    _cache.clear()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
