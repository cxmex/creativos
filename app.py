import os
import time
import asyncio
from datetime import datetime, timedelta
from collections import defaultdict
import httpx
from fastapi import FastAPI, Request, UploadFile, File, Form
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


async def sb(client, method, endpoint, params=None, json_data=None):
    url = f"{SUPABASE_URL}{endpoint}"
    r = await client.request(method.upper(), url, headers=HEADERS,
                             params=params, json=json_data, timeout=30)
    return r.json() if r.content else None


async def _list_bucket_folder(client, bucket, prefix):
    r = await client.post(
        f"{SUPABASE_URL}/storage/v1/object/list/{bucket}",
        headers=HEADERS, json={"prefix": prefix, "limit": 1}, timeout=10
    )
    if r.status_code != 200:
        return None
    files = r.json()
    if files and isinstance(files, list) and files[0].get("id"):
        return f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{prefix}{files[0]['name']}"
    return None


async def _list_all_files(client, bucket, prefix):
    r = await client.post(
        f"{SUPABASE_URL}/storage/v1/object/list/{bucket}",
        headers=HEADERS, json={"prefix": prefix, "limit": 200}, timeout=10
    )
    if r.status_code != 200:
        return []
    items = r.json()
    if not isinstance(items, list):
        return []
    return [
        f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{prefix}{item['name']}"
        for item in items if item.get("id")
    ]


async def _fetch_all_inventario1(client):
    """Concurrent pagination through all inventario1 rows."""
    count_resp = await client.get(
        f"{SUPABASE_URL}/rest/v1/inventario1",
        headers={**HEADERS, "Prefer": "count=exact"},
        params={"select": "id", "limit": "1"}, timeout=15,
    )
    cr = count_resp.headers.get("content-range", "0-0/0")
    total = int(cr.split("/")[-1]) if "/" in cr else 0
    if total == 0:
        return []

    offsets = list(range(0, total, 1000))

    async def fetch_page(offset):
        return await sb(client, "GET", "/rest/v1/inventario1",
                        params={"select": "estilo_id,color,color_id,terex1,terex2",
                                "limit": "1000", "offset": str(offset)})

    pages = await asyncio.gather(*[fetch_page(o) for o in offsets])
    rows = []
    for page in pages:
        if page:
            rows.extend(page)
    return rows


async def _build_estilos_from_rest():
    async with httpx.AsyncClient() as client:
        estilos, stock_rows = await asyncio.gather(
            sb(client, "GET", "/rest/v1/inventario_estilos",
               params={"select": "id,nombre,supplier", "limit": "1000"}),
            _fetch_all_inventario1(client),
        )

    # agg[estilo_id][color_text] = {"t1": int, "t2": int, "color_id": int|None}
    agg = defaultdict(lambda: defaultdict(lambda: {"t1": 0, "t2": 0, "color_id": None}))
    for r in (stock_rows or []):
        eid = r.get("estilo_id")
        if not eid:
            continue
        color = (r.get("color") or "SIN COLOR").strip().upper()
        agg[eid][color]["t1"] += r.get("terex1") or 0
        agg[eid][color]["t2"] += r.get("terex2") or 0
        if r.get("color_id") and agg[eid][color]["color_id"] is None:
            agg[eid][color]["color_id"] = r["color_id"]

    result = []
    for e in (estilos or []):
        eid = e["id"]
        colors = [
            {"color": c, "color_id": v["color_id"],
             "t1": v["t1"], "t2": v["t2"], "total": v["t1"] + v["t2"]}
            for c, v in agg.get(eid, {}).items()
        ]
        colors.sort(key=lambda x: x["total"], reverse=True)
        t1 = sum(c["t1"] for c in colors)
        t2 = sum(c["t2"] for c in colors)
        result.append({
            "id": eid,
            "nombre": e["nombre"],
            "proveedor": e.get("supplier") or "",
            "colors": colors,
            "t1": t1, "t2": t2,
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

    # Step 1: get all estilo_id folders in images_estilos (underscore bucket)
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SUPABASE_URL}/storage/v1/object/list/images_estilos",
            headers=HEADERS, json={"prefix": "", "limit": 500}, timeout=15
        )
        folders = [x["name"] for x in (r.json() if r.status_code == 200 else []) if x.get("name")]

        # Step 2: get first file from each folder concurrently
        urls = await asyncio.gather(*[
            _list_bucket_folder(client, "images_estilos", f"{fid}/")
            for fid in folders
        ])

    thumbs = {fid: url for fid, url in zip(folders, urls) if url}
    cache_set("thumbs", thumbs)
    return thumbs


@app.get("/api/color-images/{estilo_id}")
async def api_color_images(estilo_id: int):
    """Returns {color_folder: {fotos: [urls], creativos: [urls]}} for all media of an estilo."""
    cache_key = f"colores_{estilo_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SUPABASE_URL}/storage/v1/object/list/images-colores",
            headers=HEADERS,
            json={"prefix": f"{estilo_id}/", "limit": 100}, timeout=15
        )
        color_folders = [x["name"] for x in (r.json() if r.status_code == 200 else []) if x.get("name")]

        async def fetch_color(cf):
            creativos, fotos, direct = await asyncio.gather(
                _list_all_files(client, "images-colores", f"{estilo_id}/{cf}/creativos/"),
                _list_all_files(client, "images-colores", f"{estilo_id}/{cf}/fotos/"),
                _list_all_files(client, "images-colores", f"{estilo_id}/{cf}/"),
            )
            # direct = files stored at root of color folder (pre-tipo uploads or manual)
            all_fotos = fotos + [u for u in direct if u not in fotos]
            return cf, {"creativos": creativos, "fotos": all_fotos}

        pairs = await asyncio.gather(*[fetch_color(cf) for cf in color_folders])

    result = {cf: val for cf, val in pairs}
    cache_set(cache_key, result)
    return result


@app.get("/api/estilos")
async def api_estilos(sort: str = "nombre"):
    cached = cache_get("estilos")
    if cached is None:
        async with httpx.AsyncClient() as client:
            result = await sb(client, "POST", "/rest/v1/rpc/get_creativos_estilos", json_data={})

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


def _ts_filename(original: str) -> str:
    ext = original.rsplit('.', 1)[-1].lower() if '.' in original else 'jpg'
    return f"{int(time.time())}.{ext}"


async def _storage_upload(bucket: str, path: str, content: bytes, content_type: str):
    upload_headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SUPABASE_URL}/storage/v1/object/{bucket}/{path}",
            headers=upload_headers, content=content, timeout=60,
        )
    return r


@app.post("/api/upload/color/{estilo_id}")
async def upload_color_photo(
    estilo_id: int,
    color_name: str = Form(...),
    color_id: int = Form(None),
    tipo: str = Form("foto"),          # "foto" or "creativo"
    file: UploadFile = File(...),
):
    content = await file.read()
    folder = str(color_id) if color_id else color_name.upper().strip().replace(" ", "_").replace("/", "_")
    tipo_dir = "creativos" if tipo == "creativo" else "fotos"
    path = f"{estilo_id}/{folder}/{tipo_dir}/{_ts_filename(file.filename)}"
    r = await _storage_upload("images-colores", path, content, file.content_type or "image/jpeg")
    if r.status_code not in (200, 201):
        return {"error": r.text, "status": r.status_code}
    _cache.pop(f"colores_{estilo_id}", None)
    return {"ok": True, "url": f"{SUPABASE_URL}/storage/v1/object/public/images-colores/{path}",
            "folder": folder, "tipo": tipo_dir}


@app.post("/api/upload/estilo/{estilo_id}")
async def upload_estilo_photo(estilo_id: int, file: UploadFile = File(...)):
    content = await file.read()
    path = f"{estilo_id}/{_ts_filename(file.filename)}"
    r = await _storage_upload("images_estilos", path, content, file.content_type or "image/jpeg")
    if r.status_code not in (200, 201):
        return {"error": r.text, "status": r.status_code}
    _cache.pop("thumbs", None)
    return {"ok": True, "url": f"{SUPABASE_URL}/storage/v1/object/public/images_estilos/{path}"}


@app.get("/api/debug/bucket")
async def debug_bucket(prefix: str = ""):
    """List raw bucket contents at any prefix — for debugging."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SUPABASE_URL}/storage/v1/object/list/images-colores",
            headers=HEADERS, json={"prefix": prefix, "limit": 200}, timeout=15
        )
        return {"status": r.status_code, "prefix": prefix, "items": r.json()}


@app.post("/api/cache/clear")
async def clear_cache():
    _cache.clear()
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
