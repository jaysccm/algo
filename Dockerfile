FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g wrangler \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONPATH=/app

COPY ./v2/requirements.txt .
COPY ./dashboard/requirements.txt ./dashboard/requirements.txt
RUN pip install --no-cache-dir -r ./requirements.txt -r ./dashboard/requirements.txt

# A release image carries the code it runs. The development Compose file may
# still bind-mount sources, but compose.release.yml uses only these copies.
COPY ./v2 ./v2
COPY ./trading ./trading
COPY ./dashboard ./dashboard
COPY ./public_dashboard ./public_dashboard
COPY ./db ./db

CMD ["python", "-m", "v2.main"]
