# Static-site image: serve the merged dashboard with Caddy. No Python build —
# the .py pipeline lives in the repo but is excluded from the image via .dockerignore.
FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
COPY . /srv
