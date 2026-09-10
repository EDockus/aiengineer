# syntax=docker/dockerfile:1
#
# docs/architecture.md section 4.4. Contains no credentials and no .npmrc: both CLIs are
# installed from the public npm registry, and every credential the agent uses is fetched at
# runtime from the platform's managed identity.

FROM python:3.14-slim

# Node LTS. Both coding CLIs are npm packages (docs/architecture.md section 4.4). Installed
# from the official tarball rather than a piped setup script so the version is pinned and
# the layer is reproducible.
ARG NODE_VERSION=22.21.1
ARG CODEX_VERSION=0.153.4
ARG CLAUDE_CODE_VERSION=2.1.266

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates curl xz-utils; \
    rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "$(dpkg --print-architecture)" in \
        amd64) node_arch=x64 ;; \
        arm64) node_arch=arm64 ;; \
        *) echo "unsupported architecture" >&2; exit 1 ;; \
    esac; \
    curl -fsSLo /tmp/node.tar.xz \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 --no-same-owner; \
    rm /tmp/node.tar.xz; \
    node --version; \
    npm --version

# Both harness CLIs, pinned. No .npmrc is written, so no registry token can end up in
# the image (docs/architecture.md section 4.4).
RUN set -eux; \
    npm install -g --no-audit --no-fund \
        "@openai/codex@${CODEX_VERSION}" \
        "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"; \
    npm cache clean --force; \
    rm -rf /root/.npm

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

# No token helper is installed. The harness is handed a pre-scoped inference token by the
# parent process (see src/ticket_to_pr/harness/__init__.py); it holds no credential it
# could use to mint another, and there is no binary in the image that would mint one for it.
#
# The ENTRYPOINT (ticket-to-pr) is a launcher, not the server: it scrubs the managed
# identity plumbing (IDENTITY_ENDPOINT/IDENTITY_HEADER, MSI_*, AZURE_CLIENT/TENANT/...) out
# of the exec-time environment and then exec's the server as the same pid, delivering the
# values on an inherited fd the server drains at startup. Because /proc/<pid>/environ is a
# process's exec-time environment, pid 1's environ stays clean for a same-uid harness that
# tries to read it -- while DefaultAzureCredential still finds the values in os.environ.
# See src/ticket_to_pr/launch.py.

# The marker that makes dev mode impossible in a deployed image. config.py refuses to
# start with T2PR_DEV_MODE set whenever this file exists (see _validate_dev_mode).
RUN set -eux; \
    mkdir -p /etc/ticket-to-pr; \
    printf 'This filesystem is a deployed ticket-to-pr container image.\n' \
        > /etc/ticket-to-pr/production; \
    chmod 0444 /etc/ticket-to-pr/production

# Two unprivileged accounts, not one. Same-uid was not a boundary: three separate security
# findings reached the managed identity because a prompt-injected harness could read what
# the server could -- /proc/<server>/environ, and the server's writable $HOME.
#
#   agent   (10001)  the server. Never executes model-generated code.
#   harness (10002)  the runner, and every coding CLI it spawns.
#   t2pr    (10003)  shared group, so one run directory can be written by the harness and
#                    read back, committed and deleted by the server. Nothing else is in it.
#
# /work stays agent-only (0750): the harness uid must not be able to plant a usercustomize
# or a .profile there. A harness's HOME is the per-run directory instead.
RUN set -eux; \
    groupadd --gid 10003 t2pr; \
    useradd --create-home --home-dir /work --uid 10001 --shell /usr/sbin/nologin agent; \
    useradd --no-create-home --home-dir /nonexistent --uid 10002 \
        --shell /usr/sbin/nologin harness; \
    usermod --append --groups t2pr agent; \
    usermod --append --groups t2pr harness; \
    mkdir -p /work; \
    chown -R agent:agent /work; \
    chmod 0750 /work

# Deliberately root, and deliberately only for as long as it takes to give the privilege
# away. ENTRYPOINT is src/ticket_to_pr/launch.py: it forks the runner, drops it to
# harness:harness, then drops itself to agent:agent before exec'ing the server. Nothing is
# root by the time either process executes anything, so the non-root requirement in
# docs/architecture.md section 4.4 -- and the Claude Code CLI's refusal to run
# bypassPermissions as root -- both still hold. Dropping is a privilege: uid 10001 cannot become 10002 (verified: EPERM), so
# the launcher has to start with it.
USER root
WORKDIR /work
ENV HOME=/work \
    T2PR_HOST=0.0.0.0 \
    T2PR_PORT=8000 \
    T2PR_SHARED_GID=10003

EXPOSE 8000

# Probes whatever port the container was actually configured with: azure.yaml sets
# T2PR_PORT=8088 (the hosted-agent runtime contract's port), which overrides the ENV
# default above, so a hard-coded 8000 here would probe a closed socket in every real
# deployment. /healthz rather than /readiness: both answer 200 from the same handler,
# and /healthz is the documented liveness route (docs/contract.md section 1).
#
# -I (isolated) is load-bearing, not tidiness: without it this interpreter imports
# usercustomize from the invoking uid's ~/.local site-packages, so a harness that had
# written one there would run its own code inside a probe that still holds the container's
# unscrubbed configuration environment -- silently, every 30 seconds, ExitCode 0.
# -I implies -E and -s; os.environ still resolves T2PR_PORT below.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -I -c "import os,urllib.request,sys; port=os.environ.get('T2PR_PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["ticket-to-pr"]
