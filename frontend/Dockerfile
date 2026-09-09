# The review UI: a static bundle served by nginx.
#
# A separate image from the backend because the build toolchain and the runtime have nothing in
# common. Nothing from the Node stage reaches the final image except the compiled assets.

# --- build ---------------------------------------------------------------------------
FROM node:22-alpine AS builder

WORKDIR /build

# Dependencies first: this layer is cached unless the manifests change.
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src

# `npm run build` type-checks before bundling, so a type error fails the image build rather than
# shipping.
RUN npm run build

# --- runtime -------------------------------------------------------------------------
# The unprivileged image runs as a non-root user and listens on 8080, which is what lets the pod
# run with runAsNonRoot and a read-only root filesystem.
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

COPY --from=builder /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080
