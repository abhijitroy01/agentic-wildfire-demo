# =============================================================================
# Fire Truck Simulator - Docker Container
# =============================================================================
# Use AWS ECR Public Gallery to avoid Docker Hub rate limits
FROM public.ecr.aws/docker/library/python:3.14-slim-trixie

# Set working directory
WORKDIR /app

# Install current security updates; runtime dependencies have binary wheels.
RUN apt-get update && apt-get upgrade -y \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better layer caching
COPY requirements.txt requirements-lock.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir --only-binary=:all: --constraint requirements-lock.txt -r requirements.txt

# Copy application code
COPY . .

# Create non-root user for security
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

USER appuser

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Run the private service. The container binds every interface explicitly, because only the API task
# reaches it through Cloud Map in private subnets; a local uvicorn run keeps uvicorn's default, 127.0.0.1.
CMD ["uvicorn", "api.private:app", "--host", "0.0.0.0", "--port", "8080"]
