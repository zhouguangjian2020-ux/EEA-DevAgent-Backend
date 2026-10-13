ARG RUNTIME_BASE=python:3.12-slim
FROM ${RUNTIME_BASE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt requirements-linux.lock ./
RUN pip install --no-cache-dir --no-deps -r requirements-linux.lock && pip check
COPY app ./app
COPY tools ./tools
COPY source-manifest.json release-info.json attest.py ./
EXPOSE 7000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "192.168.64.3", "--port", "7000"]
