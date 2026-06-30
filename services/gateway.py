"""Public gateway: serves the static demo site and reverse-proxies /api/*
requests to the internal probe/ledger/attestation/escrow services. This is
the only process exposed to the internet — the 4 backend services bind to
127.0.0.1 and are unreachable from outside the container.
"""
import os
import sys

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

app = FastAPI(title="Beacon Gateway")

STATIC_DIR = os.path.join(config.BASE_DIR, "static")

UPSTREAMS = {
    "probe": config.PROBE_ENGINE_URL,
    "ledger": config.LEDGER_API_URL,
    "attestation": config.ATTESTATION_URL,
    "escrow": config.ESCROW_GATE_URL,
}

HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}


async def proxy(upstream_base: str, path: str, request: Request) -> Response:
    url = f"{upstream_base}/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS}
    body = await request.body()

    # Generous timeout: /v1/probe can legitimately take close to
    # PROBE_TIMEOUT_SECONDS (5s, probing the target) plus NOUS_TIMEOUT_SECONDS
    # (25s, the live Nemotron 3 Ultra call) before responding.
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.request(
                request.method,
                url,
                params=request.query_params,
                headers=headers,
                content=body,
            )
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"error": f"upstream unreachable: {e}"})

    response_headers = {
        k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
    }
    return Response(content=resp.content, status_code=resp.status_code, headers=response_headers)


@app.api_route("/api/probe/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_probe(path: str, request: Request):
    return await proxy(UPSTREAMS["probe"], path, request)


@app.api_route("/api/ledger/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_ledger(path: str, request: Request):
    return await proxy(UPSTREAMS["ledger"], path, request)


@app.api_route("/api/attestation/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_attestation(path: str, request: Request):
    return await proxy(UPSTREAMS["attestation"], path, request)


@app.api_route("/api/escrow/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_escrow(path: str, request: Request):
    return await proxy(UPSTREAMS["escrow"], path, request)


@app.get("/health")
async def health():
    results = {}
    async with httpx.AsyncClient(timeout=2.0) as client:
        for name, base in UPSTREAMS.items():
            try:
                r = await client.get(f"{base}/health")
                results[name] = r.status_code == 200
            except httpx.HTTPError:
                results[name] = False
    overall_ok = all(results.values())
    return JSONResponse(
        status_code=200 if overall_ok else 503,
        content={"status": "ok" if overall_ok else "degraded", "service": "gateway", "upstreams": results},
    )


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "demo.html"))


@app.get("/pricing")
async def pricing():
    return FileResponse(os.path.join(STATIC_DIR, "pricing.html"))


# Static site mounted last so /api/*, /health, and / above take precedence.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
