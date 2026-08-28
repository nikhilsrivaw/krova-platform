# Krova API - for the EC2 test deploy. Matches local dev's Python 3.13.
FROM python:3.13-slim

WORKDIR /app

# System deps for asyncpg/cryptography wheels that sometimes need building.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
