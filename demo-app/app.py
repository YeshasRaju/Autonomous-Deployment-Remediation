import os
import random
import time
import logging

from flask import Flask, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

app = Flask(__name__)
logger = logging.getLogger(__name__)

APP_VERSION = os.environ.get("APP_VERSION", "v1")

REQUEST_COUNT = Counter(
    "payment_service_requests_total",
    "Total HTTP requests handled by the payment service.",
    ["status", "version"],
)
REQUEST_LATENCY = Histogram(
    "payment_service_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["version"],
)


@app.before_request
def start_request_timer():
    if request.path not in ("/metrics", "/health"):
        request.request_start_time = time.perf_counter()


@app.after_request
def record_request_metrics(response):
    if request.path not in ("/metrics", "/health"):
        REQUEST_COUNT.labels(str(response.status_code), APP_VERSION).inc()
        REQUEST_LATENCY.labels(APP_VERSION).observe(
            time.perf_counter() - request.request_start_time
        )
    return response


@app.route("/")
def index():
    if APP_VERSION == "v2" and random.random() < 0.30:
        # Simulate a flaky database connection timeout (~30% of requests)
        time.sleep(2.5)
        logger.error("database connection timeout")
        return jsonify({"error": "database connection timeout"}), 500

    return jsonify({"status": "ok", "version": APP_VERSION})


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "version": APP_VERSION})


@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
