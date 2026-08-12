# NovoMCP NEB Engine

Transition-state search service for the NovoMCP engine. Runs Nudged Elastic Band (NEB) optimization with ASE, using xTB (semi-empirical GFN2-xTB, via subprocess) for energies and forces.

## Features

- **NEB transition-state search**: interpolates a reaction path between reactant and product geometries and optimizes the band to locate the saddle point.
- **xTB backend**: energies and forces from the xTB binary (installed in the container image).
- **Async jobs**: long runs execute in the background with status and result polling; job state is stored in Redis under the `novomcp:` key namespace. With no `REDIS_URL` set, jobs run synchronously.

## API Endpoints

- `GET /health` - Health check
- `GET /` - Service information
- `POST /api/qm-neb` - Submit a NEB transition-state job
- `GET /status/{job_id}` - Job status
- `GET /results/{job_id}` - Job result

`POST` accepts an optional `X-Api-Key` header (required only when `NEB_API_KEY` is set).

## Configuration

| Variable | Description |
|---|---|
| `PORT` | Service port (default: `8032`) |
| `REDIS_URL` | Redis connection for async job tracking (optional; unset = synchronous only) |
| `NEB_API_KEY` | Optional API key; when set, `POST` requires a matching `X-Api-Key` header |

## Deployment

The service is a single stateless container (xTB is baked into the image).

```bash
# Pull and run the published image
docker run -p 8032:8032 ghcr.io/novomcp/novomcp-neb:latest

# Or build from source
docker build -t novomcp-neb .
docker run -p 8032:8032 novomcp-neb
```

Point the NovoMCP engine at this service by setting `NOVOMCP_NEB_URL` to its URL.

## License

Code is licensed under the Apache License 2.0 (see `LICENSE`). Runtime components (xTB, ASE) are under their own licenses; see `NOTICE`.
