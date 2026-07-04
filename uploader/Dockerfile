# stdlib-only Python uploader: interpreter + one file. Content bind-mounted at runtime.
FROM python:3.12-alpine
COPY server.py /app/server.py
ENV PORT=80
EXPOSE 80
CMD ["python3", "/app/server.py"]
