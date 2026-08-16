# Sandbox image for running a configured agent under test.
#
# The agent CLI must run *inside* the sandbox, not on the host: an agent
# configured with bash/edit permissions is exactly the configuration worth
# testing and the least safe to run on a developer's machine. The host's own
# OpenCode binary cannot be uploaded — it is a platform-specific executable —
# so the Linux build is installed here instead.
#
# Build is handled by OpenShell:
#   sandbox:
#     image: docker/agent-sandbox.Dockerfile
# which resolves to `openshell sandbox create --from <this file>`.
FROM ghcr.io/nvidia/openshell-community/sandboxes/base:latest

USER root

# Pinned so a sandbox rebuild cannot silently change the agent runtime under an
# evaluation. Bump deliberately, and re-record the version in run artifacts.
ARG OPENCODE_VERSION=1.4.3
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}" \
    && opencode --version

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
