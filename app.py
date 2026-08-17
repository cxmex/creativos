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
    return thumbs


@app.get("/api/estilos")
async def api_estilos(sort: str = "nombre"):
    async with httpx.AsyncClient() as client:
        estilos, stock_rows = await asyncio.gather(
            sb(client, "GET", "/rest/v1/inventario_estilos",
               params={"select": "id,nombre,proveedor", "limit": "1000"}),
            sb(client, "GET", "/rest/v1/inventario1",
               params={"select": "estilo_id,color,terex1,terex2", "limit": "60000"}),
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

    if sort == "stock_total":
        result.sort(key=lambda x: x["total"], reverse=True)
    elif sort == "stock_t1":
        result.sort(key=lambda x: x["t1"], reverse=True)
    elif sort == "stock_t2":
        result.sort(key=lambda x: x["t2"], reverse=True)
    elif sort == "colores":
        result.sort(key=lambda x: x["num_colors"], reverse=True)
    else:
        result.sort(key=lambda x: x["nombre"])

    return result


@app.get("/api/ventas-por-estilo")
async def api_ventas_por_estilo(dias: int = 30):
    cutoff = (datetime.utcnow() - timedelta(days=dias)).date().isoformat()
    async with httpx.AsyncClient() as client:
        bc_map_rows, v1, v2 = await asyncio.gather(
            sb(client, "GET", "/rest/v1/inventario1",
               params={"select": "barcode,estilo_id", "limit": "60000"}),
            sb(client, "GET", "/rest/v1/ventas_terex1",
               params={"select": "barcode,qty", "fecha": f"gte.{cutoff}", "limit": "50000"}),
            sb(client, "GET", "/rest/v1/ventas_terex2",
               params={"select": "barcode,qty", "fecha": f"gte.{cutoff}", "limit": "50000"}),
        )

    bc_map = {r["barcode"]: r["estilo_id"] for r in (bc_map_rows or []) if r.get("barcode")}
    ventas = defaultdict(int)
    for r in ((v1 or []) + (v2 or [])):
        eid = bc_map.get(r.get("barcode"))
        if eid:
            ventas[eid] += r.get("qty") or 1

    return dict(ventas)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
