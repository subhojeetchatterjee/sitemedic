FROM python:3.11-slim

# Install gcloud CLI for Cloud Run actions
RUN apt-get update && apt-get install -y curl apt-transport-https gnupg && \
    curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg && \
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
        > /etc/apt/sources.list.d/google-cloud-sdk.list && \
    apt-get update && apt-get install -y google-cloud-cli && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cache layer)
COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy agent source
COPY agent/ .

# Copy environment config files (referenced at runtime as /config/environments/*.yaml)
COPY config/ /config/

ENV PORT=8080
EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
