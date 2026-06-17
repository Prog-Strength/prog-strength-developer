# Run-history Prometheus exporter (manager docker-compose service).
#
# Build context is the REPO ROOT (set in monitoring/docker-compose.yml) so
# this can COPY the `fleet` package the exporter depends on. Scans the
# prog-strength-developer-runs DynamoDB table and serves aggregate gauges
# on :9102 for Prometheus to scrape over the compose network.
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir 'prometheus_client>=0.20' 'boto3>=1.34'

# Only what the exporter imports: the fleet package + the exporter shell.
COPY fleet/ ./fleet/
COPY bootstrap/ddb_exporter.py ./

ENV PYTHONUNBUFFERED=1

# AWS_REGION and DDB_EXPORTER_* come from the compose service env; AWS
# credentials arrive via the manager instance profile over IMDS.
CMD ["python", "ddb_exporter.py"]
