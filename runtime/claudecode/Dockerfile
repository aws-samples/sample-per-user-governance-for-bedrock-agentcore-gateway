# Claude Code runtime image for AgentCore Runtime (build with --platform linux/arm64).
#
# Base is the Amazon ECR Public mirror of the official Python image, pinned by
# the digest of its multi-arch index so a rebuild resolves the same bytes.
# Refresh the digest with:
#   docker buildx imagetools inspect public.ecr.aws/docker/library/python:3.12-slim
FROM public.ecr.aws/docker/library/python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17

# Apply OS security updates available at build time, then add the only extra
# package this image needs: curl, to fetch the Claude Code installer. The
# pinned base is only rebuilt on the upstream cadence, so the upgrade clears
# fixable CVEs in its layers without giving up the reproducible digest.
#
# perl-base then goes, and it is the last thing removed because apt and dpkg
# maintainer scripts are allowed to use it. It arrives with the Debian base
# rather than with anything installed here, it carries CVEs that Debian has not
# published a fix for, and no component of this image links against it or
# executes a perl script: the agent is the native Claude Code binary and the
# server is CPython. Debian flags it Essential, hence --force-remove-essential;
# useradd, pip, and the Claude Code CLI are all verified to work without it.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && dpkg --purge --force-remove-essential perl-base

# Fully pinned, transitive set included. requirements.txt documents the range
# the pins were resolved from and how to move them forward.
#
# pip itself is then removed. It is a build-time tool that nothing in the
# running container invokes, and leaving it installed keeps its own advisories
# in every scan of this image. Upgrading it instead is a worse trade: the pip
# releases that fix those advisories vendor a msgpack and a setuptools that
# scanners report at HIGH, so the upgrade swaps four MEDIUMs for three HIGHs.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && pip uninstall --yes pip

# Non-root user: Claude Code refuses --dangerously-skip-permissions as root.
RUN useradd --create-home --uid 1001 agent

# Install native Claude Code as the runtime user, pinned to one release rather
# than whatever "latest" is on build day. install.sh is Anthropic's own
# installer for the CLI; it takes [stable|latest|VERSION] as its first
# argument and verifies the downloaded binary against a published SHA256.
# pipefail so a failed download cannot silently pipe an empty script into bash.
#
# Read the trust boundary before adopting this line. The CLI build is pinned and
# checksum-verified, but the installer script itself is fetched over TLS at build
# time and executed unverified, so this build trusts claude.ai to serve the same
# script it served yesterday. That is the vendor's documented install path, and
# the exposure is bounded to build time in an image you build yourself. If your
# supply-chain policy does not allow executing a remote script, replace this line
# with either a vendored copy of the installer committed alongside this
# Dockerfile, or an install from a package registry you already mirror, and pin
# whichever you choose by digest.
ARG CLAUDE_CODE_VERSION=2.1.231
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
USER agent
ENV HOME=/home/agent
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}"

USER root
RUN ln -s /home/agent/.local/bin/claude /usr/local/bin/claude \
    && /usr/local/bin/claude --version

WORKDIR /app
COPY app.py /app/app.py
USER agent
ENV CLAUDE_BIN=/usr/local/bin/claude

EXPOSE 8080
# The runtime SDK serves /ping on 8080; AgentCore also health-checks the
# container externally, so this covers plain-Docker runs of the image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=3)" || exit 1
CMD ["python", "/app/app.py"]
