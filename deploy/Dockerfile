# syntax=docker/dockerfile:1
# Public Pulseboard image, Profile A (ADDENDUM-01 §1A.6, §5.1). Contains no company material;
# deploy/Dockerfile.corp wraps it for the corporate registry.
#
# Build (always linux/amd64, the Diego cell architecture):
#   docker buildx build --platform linux/amd64 -f deploy/Dockerfile -t pulseboard:dev --load .
#
# Behind TLS interception on the build machine (Norton Web/Mail Shield, a corporate proxy), uv
# inside the build cannot see the host certificate store. Pass certifi plus the intercepting root
# as a BuildKit secret, never COPY (ADDENDUM-01 §5.3, L16); uv then uses native TLS with it:
#   --secret id=build_ca,src=<path-to-bundle.pem>

ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# ---- builder: uv resolves from the lock file, wheels only for third-party packages ----------
FROM ghcr.io/astral-sh/uv:0.9.10 AS uv
FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv

# Precompiled .pyc: the runtime user cannot write them, so without them every start compiles each
# imported module again (slower start, more memory).
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Only the hosted extra (no PDF extras: PDF export is deferred). matplotlib and the packages only it
# needs are left out: pyjobshop requires matplotlib for pyjobshop.plot, which nothing imports
# (docs/DEPENDENCIES.md). scripts/image_smoke.py checks they are absent and that planning works.
ARG NOT_INSTALLED="--no-install-package matplotlib --no-install-package pillow \
--no-install-package fonttools --no-install-package kiwisolver --no-install-package contourpy \
--no-install-package pyparsing --no-install-package cycler"

WORKDIR /src

COPY pyproject.toml uv.lock ./
# Third-party packages: the build fails if any of them would need compiling.
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export UV_NATIVE_TLS=true SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --locked --no-dev --extra hosted $NOT_INSTALLED --no-install-project --no-build

COPY README.md LICENSE ./
COPY src/ ./src/
# Pulseboard itself, installed as a regular (non-editable) package.
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export UV_NATIVE_TLS=true SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --locked --no-dev --extra hosted $NOT_INSTALLED --no-editable

# Files nothing imports at runtime: test suites, C/C++ headers and sources, Cython sources and
# type stubs. Package data (Plotly's validators and plotly.min.js) and dist-info stay.
RUN cd /app/.venv/lib/python3.12/site-packages \
    && find . -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + \
    && find . -type f \( -name '*.h' -o -name '*.hpp' -o -name '*.hxx' -o -name '*.c' \
        -o -name '*.cc' -o -name '*.cpp' -o -name '*.pyx' -o -name '*.pxd' -o -name '*.pxi' \
        -o -name '*.pyi' \) -delete

# ---- runtime: only the virtual environment and the start script (no migration files) --------
FROM ${PYTHON_IMAGE} AS runtime

# The base image's pip, ensurepip and IDLE are unused (the venv has no pip). Deleting them in a
# later layer saves no space, as the base layers stay; it keeps the tools out of the container.
RUN groupadd --gid 1000 pulseboard \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin pulseboard \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip /usr/local/lib/python3.12/site-packages/pip-* \
        /usr/local/lib/python3.12/ensurepip /usr/local/lib/python3.12/idlelib \
        /usr/local/bin/pip /usr/local/bin/pip3* /usr/local/bin/idle*

COPY --from=builder /app/.venv /app/.venv
COPY --chmod=0755 deploy/start.sh /app/start.sh

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_SYNC=1 \
    PULSEBOARD_TRANSPORT=http \
    PULSEBOARD_HOST=0.0.0.0

USER 1000:1000
WORKDIR /home/pulseboard
EXPOSE 8765

# No ENTRYPOINT (§5.1): every process names its own command. The manifest repeats this one.
CMD ["/app/start.sh"]
