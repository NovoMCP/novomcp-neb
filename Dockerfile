FROM python:3.11-slim-bullseye

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y \
    curl wget xz-utils libgomp1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install xTB binary (same as novomcp-qm — proven, works)
RUN mkdir -p /opt/xtb && \
    wget -q "https://github.com/grimme-lab/xtb/releases/download/v6.7.1/xtb-6.7.1-linux-x86_64.tar.xz" -O /tmp/xtb.tar.xz && \
    tar -xJf /tmp/xtb.tar.xz -C /opt/xtb --strip-components=1 && \
    rm /tmp/xtb.tar.xz

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
