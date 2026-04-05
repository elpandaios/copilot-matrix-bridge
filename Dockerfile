FROM python:3.12-slim

# Install libolm for E2E encryption + Node.js for copilot CLI
RUN apt-get update && \
    apt-get install -y --no-install-recommends libolm-dev curl git && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g @github/copilot && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Crypto keys and bridge DB persist via volumes
VOLUME ["/data", "/root/.copilot"]

CMD ["python", "bridge.py"]
