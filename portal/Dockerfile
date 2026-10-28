# The client portal, built, behind Caddy -- which also proxies /api and /panel
# to the API, so the whole product answers on one origin (deploy/Caddyfile).
# Built from the repo root, for the Caddyfile; the root .dockerignore keeps
# node_modules, dist, .env and data/ out of the context.

FROM node:22-alpine AS build
WORKDIR /portal
COPY portal/package.json portal/package-lock.json ./
RUN npm ci
COPY portal/ ./
# The production build: type-checked, minified, mocks left out.
RUN npm run build

FROM caddy:2-alpine
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /portal/dist /srv
# Not root, like the other images: it listens on 8080 inside the container,
# which needs no privilege, and writes only its own data and config. The
# official binary carries the capability to bind low ports; a container
# with every capability dropped refuses to run a binary that asks for one,
# so it is taken off -- 8080 never needed it.
RUN apk add --no-cache libcap \
    && setcap -r /usr/bin/caddy \
    && apk del libcap \
    && mkdir -p /data/caddy /config/caddy \
    && chown -R 1000:1000 /data /config
USER 1000:1000
EXPOSE 8080
