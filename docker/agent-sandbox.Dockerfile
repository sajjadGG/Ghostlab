# Sandbox image for running a configured agent under test.
#
# The agent CLI must run *inside* the sandbox, not on the host: an agent
# configured with bash/edit permissions is exactly the configuration worth
# testing and the least safe to run on a developer's machine. The host's own
# Agent binaries cannot be uploaded — they are platform-specific executables —
# so the Linux builds are installed here instead.
#
# Build is handled by OpenShell:
#   sandbox:
#     image: docker/agent-sandbox.Dockerfile
# which resolves to `openshell sandbox create --from <this file>`.
FROM ghcr.io/nvidia/openshell-community/sandboxes/base@sha256:aeef1c63f00e2913ea002ccb3aaf925f338b5c5d70e63576f0d95c16a138044e

USER root

# Pinned so a sandbox rebuild cannot silently change the agent runtime under an
# evaluation. Bump deliberately, and re-record the version in run artifacts.
ARG OPENCODE_VERSION=1.4.3
ARG COPILOT_VERSION=1.0.80
RUN npm install -g \
        "opencode-ai@${OPENCODE_VERSION}" \
        "@github/copilot@${COPILOT_VERSION}" \
    && opencode --version \
    && copilot --version

# OpenCode keeps credentials and session state under XDG paths. Fixing them
# outside $HOME keeps them off the uploaded workspace, so an agent with `edit`
# permission cannot read or rewrite its own auth file.
ENV XDG_DATA_HOME=/opt/agent/share \
    XDG_CONFIG_HOME=/opt/agent/config \
    XDG_CACHE_HOME=/opt/agent/cache
RUN mkdir -p /opt/agent/share/opencode /opt/agent/config /opt/agent/cache \
    && chown -R 998:998 /opt/agent \
    && chmod 700 /opt/agent/share/opencode

USER 998
WORKDIR /sandbox
