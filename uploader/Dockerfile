# stdlib-only Python uploader: interpreter + one file. Content bind-mounted at runtime.
FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
COPY server.py /app/server.py
ENV PORT=80
EXPOSE 80
CMD ["python3", "/app/server.py"]
