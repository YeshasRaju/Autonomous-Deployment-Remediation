import os
import random
import time

from flask import Flask, jsonify

app = Flask(__name__)

APP_VERSION = os.environ.get("APP_VERSION", "v1")


@app.route("/")
def index():
    if APP_VERSION == "v2" and random.random() < 0.30:
        # Simulate a flaky database connection timeout (~30% of requests)
        time.sleep(2.5)
        return jsonify({"error": "database connection timeout"}), 500

    return jsonify({"status": "ok", "version": APP_VERSION})


@app.route("/health")
def health():
    return jsonify({"status": "healthy", "version": APP_VERSION})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
