FROM python:3.11-slim

# Prevent Python from writing .pyc files & buffer stdout/stderr
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# M-11: Create non-root user for container security hardening
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy application source
COPY . .
RUN chown -R appuser:appuser /app

USER 10001

# Cloud Run injects PORT environment variable (default 8080)
ENV PORT=8080

# Run functions-framework web server
CMD exec functions-framework --target=main --port=${PORT}
