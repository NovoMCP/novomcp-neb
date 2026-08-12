"""
NovoMCP NEB Service

Nudged Elastic Band transition state search using xTB binary + ASE NEB.
xTB runs via subprocess (same pattern as novomcp-qm), wrapped in a
custom ASE Calculator for use with ASE's CI-NEB optimizer.

Exposes: /api/qm-neb
"""

import asyncio
import json
import os
import logging
import time
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from app.engines.neb import run_neb

logging.basicConfig(
    format="[NovoMCP] %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("novomcp-neb")

PORT = int(os.getenv("PORT", "8032"))
API_KEY = os.getenv("NEB_API_KEY", "")
REDIS_URL = os.getenv("REDIS_URL", "")

# Redis client (initialized on startup)
redis_client = None

app = FastAPI(
    title="NovoMCP NEB Engine",
    description="NEB transition state search: activation barriers via GFN2-xTB CI-NEB",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Auth ---

def _check_key(key: Optional[str]):
    if API_KEY and key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# --- Request/Response Models ---

class NEBRequest(BaseModel):
    reactant_xyz: str = Field(..., description="XYZ geometry of optimized reactant")
    product_xyz: str = Field(..., description="XYZ geometry of optimized product")
    n_images: int = Field(8, description="Number of intermediate images (default 8)", ge=3, le=20)
    charge: int = Field(0, description="Molecular charge")
    uhf: int = Field(0, description="Unpaired electrons")
    solvent: Optional[str] = Field(None, description="ALPB solvent model")
    fmax: float = Field(0.05, description="Force convergence in eV/A")
    max_steps: int = Field(200, description="Max optimizer steps", ge=10, le=1000)
    climb: bool = Field(True, description="Use climbing image NEB for exact TS")

class NEBResponse(BaseModel):
    activation_energy_kcal: Optional[float] = None
    activation_energy_ev: Optional[float] = None
    reverse_barrier_kcal: Optional[float] = None
    reverse_barrier_ev: Optional[float] = None
    ts_energy_ev: Optional[float] = None
    reactant_energy_ev: Optional[float] = None
    product_energy_ev: Optional[float] = None
    ts_geometry_xyz: Optional[str] = None
    mep_energies_ev: Optional[list[float]] = None
    mep_energies_kcal: Optional[list[float]] = None
    n_images: int
    converged: bool
    n_steps: int
    method: str
    wall_time_seconds: Optional[float]
    warnings: list[str] = []


# --- Redis Job Tracking ---

async def _init_redis():
    global redis_client
    if not REDIS_URL:
        logger.warning("No REDIS_URL configured — NEB jobs run synchronously only")
        return
    try:
        import redis.asyncio as aioredis
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info("Redis connected for async job tracking")
    except Exception as e:
        logger.warning(f"Redis not available: {e}")
        redis_client = None


async def update_job_status(job_id: str, status: str, progress: dict):
    if not redis_client:
        return
    try:
        key = f"novomcp:job:{job_id}"
        await redis_client.hset(key, mapping={
            "job_id": job_id,
            "status": status,
            "progress": json.dumps(progress),
            "last_updated": datetime.utcnow().isoformat(),
        })
        await redis_client.expire(key, 86400)
    except Exception as e:
        logger.error(f"Failed to update job status: {e}")


async def complete_job(job_id: str, result: dict):
    if not redis_client:
        return
    try:
        now = datetime.utcnow().isoformat()
        key = f"novomcp:job:{job_id}"
        await redis_client.hset(key, mapping={
            "job_id": job_id,
            "status": "completed",
            "completed_at": now,
            "result": json.dumps(result),
            "progress": json.dumps({"percentage": 100, "message": "Completed", "step": "completed"}),
            "last_updated": now,
        })
        await redis_client.expire(key, 604800)
        await redis_client.set(f"novomcp:job_result:{job_id}", json.dumps(result), ex=604800)
    except Exception as e:
        logger.error(f"Failed to complete job: {e}")


async def fail_job(job_id: str, error: str):
    if not redis_client:
        return
    try:
        now = datetime.utcnow().isoformat()
        key = f"novomcp:job:{job_id}"
        await redis_client.hset(key, mapping={
            "job_id": job_id,
            "status": "failed",
            "completed_at": now,
            "error": error,
            "progress": json.dumps({"percentage": 0, "message": f"Failed: {error}", "step": "failed"}),
            "last_updated": now,
        })
        await redis_client.expire(key, 86400)
    except Exception as e:
        logger.error(f"Failed to mark job as failed: {e}")


# --- Startup ---

@app.on_event("startup")
async def startup_event():
    logger.info("Starting NovoMCP NEB Engine...")
    from app.engines.neb import is_available
    if is_available():
        logger.info("xTB binary: available (GFN2-xTB NEB ready)")
    else:
        logger.error("xTB binary NOT found — NEB will not work")
    await _init_redis()


# --- Health ---

@app.get("/health")
async def health():
    from app.engines.neb import is_available
    xtb_ok = is_available()

    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=200 if xtb_ok else 503,
        content={
            "status": "healthy" if xtb_ok else "unhealthy",
            "service": "novomcp-neb",
            "version": "2.0.0",
            "port": PORT,
            "engines": {
                "xtb": {"available": xtb_ok, "method": "GFN2-xTB (subprocess)"},
                "neb": {"available": xtb_ok, "method": "CI-NEB (ASE)"},
            },
            "async_jobs": redis_client is not None,
        },
    )


@app.get("/")
async def root():
    return {"service": "novomcp-neb", "version": "1.0.0"}


# --- NEB Transition State Search (Async) ---

# Concurrency limiter — NEB is CPU-bound (xTB subprocess per image per step)
neb_semaphore = asyncio.Semaphore(2)


async def _run_neb_job(job_id: str, req: NEBRequest):
    """Background task for NEB transition state search."""
    async with neb_semaphore:
        try:
            await update_job_status(job_id, "running", {
                "percentage": 10,
                "message": f"Running CI-NEB with {req.n_images} images, max {req.max_steps} steps",
                "step": "computing",
            })

            result = run_neb(
                reactant_xyz=req.reactant_xyz,
                product_xyz=req.product_xyz,
                n_images=req.n_images,
                charge=req.charge,
                uhf=req.uhf,
                solvent=req.solvent,
                fmax=req.fmax,
                max_steps=req.max_steps,
                climb=req.climb,
            )

            if not result.success:
                await fail_job(job_id, result.error or "NEB failed")
                return

            job_result = {
                "activation_energy_kcal": result.activation_energy_kcal,
                "activation_energy_ev": result.activation_energy_ev,
                "reverse_barrier_kcal": result.reverse_barrier_kcal,
                "reverse_barrier_ev": result.reverse_barrier_ev,
                "ts_energy_ev": result.ts_energy_ev,
                "reactant_energy_ev": result.reactant_energy_ev,
                "product_energy_ev": result.product_energy_ev,
                "ts_geometry_xyz": result.ts_geometry_xyz,
                "mep_energies_ev": result.mep_energies_ev,
                "mep_energies_kcal": result.mep_energies_kcal,
                "n_images": result.n_images,
                "converged": result.converged,
                "n_steps": result.n_steps,
                "method": result.method,
                "wall_time_seconds": result.wall_time_seconds,
                "warnings": result.warnings,
            }

            await complete_job(job_id, job_result)
            logger.info(f"NEB job {job_id} completed: barrier={result.activation_energy_kcal} kcal/mol, {result.wall_time_seconds}s")

        except Exception as e:
            logger.exception(f"NEB job {job_id} failed: {e}")
            await fail_job(job_id, str(e))


@app.post("/api/qm-neb")
async def qm_neb(req: NEBRequest, x_api_key: Optional[str] = Header(None)):
    _check_key(x_api_key)

    if not redis_client:
        # Fallback: synchronous execution (no Redis)
        result = run_neb(
            reactant_xyz=req.reactant_xyz,
            product_xyz=req.product_xyz,
            n_images=req.n_images,
            charge=req.charge,
            uhf=req.uhf,
            solvent=req.solvent,
            fmax=req.fmax,
            max_steps=req.max_steps,
            climb=req.climb,
        )
        if not result.success:
            raise HTTPException(status_code=500, detail=result.error or "NEB failed")
        return NEBResponse(
            activation_energy_kcal=result.activation_energy_kcal,
            activation_energy_ev=result.activation_energy_ev,
            reverse_barrier_kcal=result.reverse_barrier_kcal,
            reverse_barrier_ev=result.reverse_barrier_ev,
            ts_energy_ev=result.ts_energy_ev,
            reactant_energy_ev=result.reactant_energy_ev,
            product_energy_ev=result.product_energy_ev,
            ts_geometry_xyz=result.ts_geometry_xyz,
            mep_energies_ev=result.mep_energies_ev,
            mep_energies_kcal=result.mep_energies_kcal,
            n_images=result.n_images,
            converged=result.converged,
            n_steps=result.n_steps,
            method=result.method,
            wall_time_seconds=result.wall_time_seconds,
            warnings=result.warnings,
        )

    # Async: generate job_id, return immediately
    job_id = f"neb_{datetime.now().strftime('%Y%m%d-%H%M%S')}_{abs(hash(req.reactant_xyz[:50])) % 100000:05d}"

    await update_job_status(job_id, "queued", {
        "percentage": 0,
        "message": "NEB transition state search queued",
        "step": "queued",
    })

    asyncio.create_task(_run_neb_job(job_id, req))

    return {
        "job_id": job_id,
        "status": "submitted",
        "service": "novomcp-neb",
        "n_images": req.n_images,
        "max_steps": req.max_steps,
        "method": f"GFN2-xTB {'CI-' if req.climb else ''}NEB",
        "estimated_minutes": max(1, req.n_images * req.max_steps // 500),
        "poll_url": f"/status/{job_id}",
    }


# --- Job Status & Results ---

@app.get("/status/{job_id}")
async def get_job_status(job_id: str):
    if not redis_client:
        raise HTTPException(status_code=503, detail="Job tracking not available (no Redis)")

    key = f"novomcp:job:{job_id}"
    data = await redis_client.hgetall(key)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    progress = json.loads(data.get("progress", "{}"))
    resp = {
        "job_id": job_id,
        "status": data.get("status"),
        "progress": progress.get("percentage", 0),
        "message": progress.get("message"),
        "step": progress.get("step"),
        "last_updated": data.get("last_updated"),
    }

    if data.get("status") == "failed":
        resp["error"] = data.get("error")

    return resp


@app.get("/results/{job_id}")
async def get_job_results(job_id: str):
    if not redis_client:
        raise HTTPException(status_code=503, detail="Job tracking not available (no Redis)")

    key = f"novomcp:job:{job_id}"
    data = await redis_client.hgetall(key)
    if not data:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    status = data.get("status")
    if status == "completed":
        return {
            "job_id": job_id,
            "status": "completed",
            "result": json.loads(data.get("result", "{}")),
        }
    elif status == "failed":
        return {
            "job_id": job_id,
            "status": "failed",
            "error": data.get("error"),
        }
    else:
        progress = json.loads(data.get("progress", "{}"))
        return {
            "job_id": job_id,
            "status": status,
            "progress": progress,
        }


if __name__ == "__main__":
    logger.info(f"Starting NovoMCP NEB Engine on port {PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
