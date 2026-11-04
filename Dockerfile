# Static podcast feed host: Caddy with the Caddyfile baked in.
# The feed content itself is NOT in the image — it is bind-mounted at runtime
# (see docker-compose.yml), so the image only ever carries config.
FROM caddy:2.11.8-alpine@sha256:3db3a09e418f86a8fce8de4bc0026fccd2b4cd925aef00cc7d843d9fdc75e442
COPY Caddyfile /etc/caddy/Caddyfile
