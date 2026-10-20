FROM python:3.12-slim
WORKDIR /app
COPY src ./src
COPY local ./local
COPY dashboard ./dashboard
COPY ar ./ar
COPY mcp ./mcp
COPY run.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
VOLUME ["/app/data"]
HEALTHCHECK --interval=15s --timeout=3s --start-period=60s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/stats', timeout=2)"
CMD ["python", "local/demo_server.py", "--host", "0.0.0.0", "--port", "8000", "--db", "/app/data/sentinel.db"]
