# Static podcast feed host: Caddy with the Caddyfile baked in.
# The feed content itself is NOT in the image — it is bind-mounted at runtime
# (see docker-compose.yml), so the image only ever carries config.
FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
