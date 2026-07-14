FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# SQLite + reports live on the mounted disk in production (see render.yaml).
RUN mkdir -p /app/data /app/reports

CMD ["bot", "run"]
