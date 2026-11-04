# stdlib-only Python uploader: interpreter + one file. Content bind-mounted at runtime.
FROM python:3.15-alpine@sha256:20a82dbc2783112ddb91d1534603f802d222a3e63e593cd002eb2ac534e70211
COPY server.py /app/server.py
ENV PORT=80
EXPOSE 80
CMD ["python3", "/app/server.py"]
