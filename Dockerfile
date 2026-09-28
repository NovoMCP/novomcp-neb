FROM python:3.11-slim-bookworm

# buildx sets TARGETARCH. The grimme-lab xtb release binary is x86_64-only, so
# arm64 (GB10/Spark, Apple Silicon) installs xtb from conda-forge (linux-aarch64,
# verified 2026-09-28). CPU service — no CUDA — so it builds multi-arch in CI.
ARG TARGETARCH

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y \
    curl wget xz-utils libgomp1 bzip2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# xtb 6.7.1: x86 = grimme-lab release binary (unchanged); arm64 = conda-forge
# (no aarch64 release binary), symlinked so XTBHOME=/opt/xtb still holds.
RUN if [ "$TARGETARCH" = "arm64" ]; then \
      curl -Ls https://micro.mamba.pm/api/micromamba/linux-aarch64/latest | tar -xj -C /usr/local bin/micromamba && \
      /usr/local/bin/micromamba create -y -p /opt/conda-xtb -c conda-forge xtb=6.7.1 && \
      /usr/local/bin/micromamba clean -afy && \
      mkdir -p /opt/xtb/bin && \
      ln -sf /opt/conda-xtb/bin/xtb /opt/xtb/bin/xtb && \
      ln -sf /opt/conda-xtb/share/xtb /opt/xtb/share ; \
    else \
      mkdir -p /opt/xtb && \
      wget -q "https://github.com/grimme-lab/xtb/releases/download/v6.7.1/xtb-6.7.1-linux-x86_64.tar.xz" -O /tmp/xtb.tar.xz && \
      tar -xJf /tmp/xtb.tar.xz -C /opt/xtb --strip-components=1 && \
      rm /tmp/xtb.tar.xz ; \
    fi

ENV PATH="/opt/xtb/bin:${PATH}"
ENV XTBHOME="/opt/xtb"
ENV OMP_NUM_THREADS=4
ENV OMP_STACKSIZE=1G

# Python deps (NO tblite — use xTB binary via subprocess instead)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Verify xTB binary + ASE NEB available
RUN xtb --version && python -c "from ase.mep.neb import NEB; print('ASE NEB: OK')"

# Application code
COPY app/ app/
COPY main.py .

# Non-root user
RUN useradd -m -u 1000 appuser && \
    mkdir -p /app/scratch && \
    chown -R appuser:appuser /app
USER appuser

ENV PORT=8032
ENV PYTHONUNBUFFERED=1
EXPOSE 8032

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8032/health || exit 1

CMD ["python", "main.py"]
