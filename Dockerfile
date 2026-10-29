# Invoice Studio cloud image, reconstructed on 2026-10-20 from the history of the live image
# us-central1-docker.pkg.dev/productintelligence-beeb3/inv-studio/app@sha256:c91d782e... (tag del-b1fb54f,
# amd64 manifest sha256:fdff1c4b...). The original build was 20+ incremental "COPY app" layers; this file
# is the single clean build that reproduces the same /opt/inv-studio tree and the same installed packages.
# The base is pinned by digest (the multi-arch index, read from the Docker Hub registry API on 2026-10-28), so a
# re-pushed tag cannot change the build. To move it: read the new index digest of the tag and replace both parts.
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ARG TORCH_VERSION=2.14.1+cpu
ARG TORCHVISION_VERSION=0.29.1+cpu
ARG OPENCV_HEADLESS_VERSION=5.0.0.93
ARG NUMPY_VERSION=2.3.5

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    HOME=/home/studio XDG_CACHE_HOME=/home/studio/.cache HF_HOME=/home/studio/.cache/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1 INV_STUDIO_DATA=/tmp/inv-studio-data PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-dejavu-core \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 studio \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/studio --shell /usr/sbin/nologin studio

WORKDIR /opt/inv-studio

# Install the dependency set first with a stub package so the layer is cached independently of app code.
# Transitive dependencies are pinned to the live image's `pip freeze` (docs/live-image-pip-freeze.txt).
# Without this, unpinned transitives drift (e.g. rapidocr 3.9.2 -> 3.10.0 breaks docling's OCR warm-up).
# The installs are pinned by version, not yet hash-checked (security review L11). `--require-hashes` needs one
# hashed lock for the whole set, including torch from download.pytorch.org (that index does publish sha256),
# and cannot take the local '.[...]' installs or the forced numpy/opencv re-install, so the five steps below
# must be rebuilt as one locked install, and that needs a full image build to prove.
COPY constraints.txt /opt/build/constraints.txt
ENV PIP_CONSTRAINT=/opt/build/constraints.txt
COPY pyproject.toml LICENSE ./
RUN mkdir -p app && touch app/__init__.py
RUN python -m pip install --no-cache-dir \
        pip==26.2.2 setuptools==84.2.1 wheel==0.48.0 \
    && python -m pip install --no-cache-dir --no-build-isolation '.[cloud,azure]' \
    && python -m pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}" \
    && python -m pip install --no-cache-dir --no-build-isolation '.[docling,paddle]' \
    && python -m pip install --no-cache-dir --force-reinstall \
        "numpy==${NUMPY_VERSION}" \
        "opencv-python-headless==${OPENCV_HEADLESS_VERSION}" \
    && python -m pip check \
    && python -c 'import azure.storage.blob, cv2, docling, paddle, psycopg; print("Cloud OCR imports are ready")'

# The "Local Claude" provider shells out to the Claude Code CLI (app/providers.py).
ARG CLAUDE_VERSION=2.1.285
ARG CLAUDE_SHA256=33dad1ec615a2e08cc78b494f05c110e49916de2c79d78ec8799ebf46b233d29
RUN python - <<'PY'
import hashlib, os, pathlib, urllib.request
url='https://downloads.claude.ai/claude-code-releases/'+os.environ['CLAUDE_VERSION']+'/linux-x64/claude'
target=pathlib.Path('/usr/local/bin/claude')
urllib.request.urlretrieve(url,target)
assert hashlib.sha256(target.read_bytes()).hexdigest()==os.environ['CLAUDE_SHA256'], 'CLI checksum mismatch'
target.chmod(0o755)
PY
ENV DISABLE_UPDATES=1
RUN claude --version

COPY app ./app
COPY samples ./samples
COPY README.md ./
COPY scripts/warm_ocr.py ./scripts/warm_ocr.py
# Run once on Azure to move the app off the database admin login (docs/AZURE.md §5a).
COPY scripts/azure_db_app_role.py ./scripts/azure_db_app_role.py
RUN mkdir -p \
        /home/studio/.cache/huggingface \
        /home/studio/.paddlex \
        /tmp/inv-studio-data/uploads \
        /tmp/inv-studio-data/exports \
        /tmp/inv-studio-data/templates \
        /tmp/inv-studio-data/work \
    && chown -R 1000:1000 /home/studio /tmp/inv-studio-data
RUN python -c 'import importlib.util,os; from pathlib import Path; p=Path(importlib.util.find_spec("rapidocr").origin).parent/"models"; p.mkdir(exist_ok=True); os.chown(p,1000,1000)'
USER 1000:1000
# Downloads the OCR models into the image and verifies every reader on the synthetic sample invoice.
RUN python scripts/warm_ocr.py
# The models are in place. From here on the app's account cannot change them (N8: PaddleX reads model settings with
# yaml.FullLoader), and no reader asks the model hub for a newer version (N6: Docling names a moving branch).
# The same two steps are in Dockerfile.app-layer, for app-only releases on an older image.
USER root
RUN set -e; \
    rapidocr="$(python -c 'import importlib.util; from pathlib import Path; print(Path(importlib.util.find_spec("rapidocr").origin).parent/"models")')"; \
    for d in /home/studio/.cache/huggingface /home/studio/.paddlex/official_models "$rapidocr"; do \
        test -d "$d"; chown -R root:root "$d"; chmod -R a-w "$d"; \
    done
USER 1000:1000
ENV HF_HUB_OFFLINE=1
EXPOSE 8080
STOPSIGNAL SIGTERM
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
