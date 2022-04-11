FROM debian:stable-slim
MAINTAINER Daniel Thorn <dthorn@mozilla.com>

# Guidelines here: https://github.com/mozilla-services/Dockerflow/blob/master/docs/building-container.md
ARG USER_ID="10001"
ARG GROUP_ID="app"
ARG HOME="/app"

ENV HOME=${HOME}
RUN groupadd --gid ${USER_ID} ${GROUP_ID} && \
    useradd --create-home --uid ${USER_ID} --gid ${GROUP_ID} --home-dir /app ${GROUP_ID}

# List packages here
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl jq && \
    apt-get autoremove -y && \
    apt-get clean

WORKDIR ${HOME}

COPY . ${HOME}

# Drop root and change ownership of the application folder to the user
RUN chown -R ${USER_ID}:${GROUP_ID} ${HOME}
USER ${USER_ID}

ARG TRIGGER_URL
ENV TRIGGER_URL=${TRIGGER_URL}

RUN : "${TRIGGER_URL:?Required build argument not set}"

ENTRYPOINT ["script/entrypoint"]
