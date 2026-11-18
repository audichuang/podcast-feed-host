# Static podcast feed host: Caddy with the Caddyfile baked in.
# The feed content itself is NOT in the image — it is bind-mounted at runtime
# (see docker-compose.yml), so the image only ever carries config.
FROM caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0
COPY Caddyfile /etc/caddy/Caddyfile
