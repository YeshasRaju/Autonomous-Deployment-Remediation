"""Rule-based Prometheus anomaly detector and Kubernetes rollback controller."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import requests


ERROR_RATE_QUERY = (
    'sum(rate(payment_service_requests_total{status="500"}[1m])) '
    '/ sum(rate(payment_service_requests_total[1m]))'
)
DEFAULT_PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
DEFAULT_ERROR_THRESHOLD = float(os.getenv("ERROR_RATE_THRESHOLD", "0.10"))
DEFAULT_POLL_INTERVAL = float(os.getenv("POLL_INTERVAL_SECONDS", "10"))
DEFAULT_CONSECUTIVE_POLLS = int(os.getenv("CONSECUTIVE_POLLS", "3"))
DEFAULT_RECENT_DEPLOYMENT_MINUTES = float(
    os.getenv("RECENT_DEPLOYMENT_MINUTES", "5")
)
DEFAULT_STABILIZATION_DELAY = float(os.getenv("STABILIZATION_DELAY_SECONDS", "70"))
DEFAULT_ROLLOUT_TIMEOUT = float(os.getenv("ROLLOUT_TIMEOUT_SECONDS", "180"))
MAX_ROLLBACK_ATTEMPTS = 2

LOGGER = logging.getLogger("anomaly-detector")


class DetectorError(RuntimeError):
    """Raised when an external dependency cannot provide a reliable result."""


def kubectl(*args: str) -> str:
    """Run kubectl and fail loudly instead of taking an unsafe action."""
    result = subprocess.run(
        ["kubectl", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise DetectorError(f"kubectl {' '.join(args)} failed: {detail}")
    return result.stdout


def query_error_rate(prometheus_url: str, simulated_rate: float | None = None) -> float:
    """Return the current error ratio from Prometheus over the trailing minute."""
    if simulated_rate is not None:
        return simulated_rate

    try:
        response = requests.get(
            f"{prometheus_url.rstrip('/')}/api/v1/query",
            params={"query": ERROR_RATE_QUERY},
            timeout=10,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        if payload.get("status") != "success":
            raise DetectorError(f"Prometheus query was not successful: {payload}")
        results = payload.get("data", {}).get("result", [])
        if not results:
            return 0.0
        rate = float(results[0]["value"][1])
        return rate if math.isfinite(rate) else 0.0
    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        raise DetectorError(f"Prometheus error-rate query failed: {error}") from error


def has_recent_deployment(
    recent_deployment_minutes: float,
    force_no_recent_deployment: bool = False,
) -> bool:
    """Use matching pod creation timestamps as the rollout signal."""
    if force_no_recent_deployment:
        return False

    try:
        payload = json.loads(
            kubectl("get", "pods", "-l", "app=payment-service", "-o", "json")
        )
        pods = payload.get("items", [])
        now = datetime.now(timezone.utc)
        for pod in pods:
            created = pod.get("metadata", {}).get("creationTimestamp")
            if not created:
                continue
            creation_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_minutes = (now - creation_time).total_seconds() / 60
            if 0 <= age_minutes <= recent_deployment_minutes:
                return True
        return False
    except (DetectorError, json.JSONDecodeError, ValueError, TypeError) as error:
        raise DetectorError(f"recent-deployment check failed: {error}") from error


def wait_for_rollout(timeout_seconds: float) -> None:
    kubectl(
        "rollout",
        "status",
        "deployment/payment-service",
        f"--timeout={int(timeout_seconds)}s",
    )


def current_deployment_revision() -> int:
    revision = kubectl(
        "get",
        "deployment/payment-service",
        "-o",
        "jsonpath={.metadata.annotations.deployment\\.kubernetes\\.io/revision}",
    ).strip()
    try:
        return int(revision)
    except ValueError as error:
        raise DetectorError(f"could not read payment-service deployment revision: {revision!r}") from error


def rollback_and_verify(
    prometheus_url: str,
    threshold: float,
    stabilization_delay: float,
    rollout_timeout: float,
    simulated_rate: float | None,
    recent_deployment_minutes: float,
    force_no_recent_deployment: bool,
) -> bool:
    target_revision = current_deployment_revision() - 1
    for attempt in range(1, MAX_ROLLBACK_ATTEMPTS + 1):
        recent_deployment = has_recent_deployment(
            recent_deployment_minutes, force_no_recent_deployment
        )
        LOGGER.info(
            "RECENT DEPLOYMENT CHECK result=%s window_minutes=%.1f",
            recent_deployment,
            recent_deployment_minutes,
        )
        if not recent_deployment:
            LOGGER.warning("NO RECENT DEPLOYMENT - ESCALATING TO HUMAN")
            return False

        LOGGER.warning("ROLLBACK STARTED attempt=%d/%d", attempt, MAX_ROLLBACK_ATTEMPTS)
        LOGGER.info(
            "ROLLBACK COMMAND kubectl rollout undo deployment/payment-service --to-revision=%d",
            target_revision,
        )
        kubectl(
            "rollout",
            "undo",
            "deployment/payment-service",
            f"--to-revision={target_revision}",
        )
        wait_for_rollout(rollout_timeout)
        LOGGER.info("ROLLOUT COMPLETE attempt=%d", attempt)
        if stabilization_delay:
            time.sleep(stabilization_delay)

        recovery_rate = query_error_rate(prometheus_url, simulated_rate)
        LOGGER.info(
            "RECOVERY CHECK error_rate=%.2f%% threshold=%.2f%%",
            recovery_rate * 100,
            threshold * 100,
        )
        if recovery_rate < threshold:
            LOGGER.info("RECOVERY VERIFIED error_rate=%.2f%%", recovery_rate * 100)
            return True
        LOGGER.error("REMEDIATION FAILED - recovery not confirmed")

    LOGGER.error("ACTION BUDGET EXHAUSTED - ESCALATING TO HUMAN")
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-url", default=DEFAULT_PROMETHEUS_URL)
    parser.add_argument("--threshold", type=float, default=DEFAULT_ERROR_THRESHOLD)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--consecutive-polls", type=int, default=DEFAULT_CONSECUTIVE_POLLS)
    parser.add_argument(
        "--recent-deployment-minutes",
        type=float,
        default=DEFAULT_RECENT_DEPLOYMENT_MINUTES,
    )
    parser.add_argument(
        "--stabilization-delay", type=float, default=DEFAULT_STABILIZATION_DELAY
    )
    parser.add_argument("--rollout-timeout", type=float, default=DEFAULT_ROLLOUT_TIMEOUT)
    parser.add_argument("--once", action="store_true", help="Poll once for smoke tests")
    parser.add_argument(
        "--simulate-error-rate",
        type=float,
        help="Use a fixed rate instead of Prometheus for controlled branch tests",
    )
    parser.add_argument(
        "--force-no-recent-deployment",
        action="store_true",
        help="Test escalation without querying pods",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    consecutive_above_threshold = 0

    while True:
        error_rate = query_error_rate(args.prometheus_url, args.simulate_error_rate)
        LOGGER.info(
            "ERROR RATE error_rate=%.2f%% threshold=%.2f%%",
            error_rate * 100,
            args.threshold * 100,
        )
        if error_rate >= args.threshold:
            consecutive_above_threshold += 1
        else:
            consecutive_above_threshold = 0

        if consecutive_above_threshold >= args.consecutive_polls:
            LOGGER.error(
                "ANOMALY DETECTED timestamp=%s error_rate=%.2f%% threshold=%.2f%%",
                datetime.now(timezone.utc).isoformat(),
                error_rate * 100,
                args.threshold * 100,
            )
            rollback_and_verify(
                args.prometheus_url,
                args.threshold,
                args.stabilization_delay,
                args.rollout_timeout,
                args.simulate_error_rate,
                args.recent_deployment_minutes,
                args.force_no_recent_deployment,
            )
            return 0

        if args.once:
            return 0
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())