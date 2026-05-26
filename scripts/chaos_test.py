#!/usr/bin/env python3
"""Chaos engineering tests for SiteMedic.

Tests system resilience by injecting faults and observing behavior:
- Latency injection
- Error rate injection
- Memory pressure (memory leak)
- Pub/Sub backlog injection

Usage:
    python scripts/chaos_test.py --environment=staging --demo-app-url=http://localhost:3000

    # Specific chaos scenario
    python scripts/chaos_test.py \
      --environment=dev \
      --demo-app-url=http://localhost:3000 \
      --scenario=latency \
      --duration=60
"""

import argparse
import json
import time
import requests
import logging
from typing import Optional

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class ChaosTest:
    """Chaos engineering test executor."""

    def __init__(self, demo_app_url: str, environment: str, api_key: str = ""):
        """Initialize chaos test.

        Args:
            demo_app_url: Demo app URL (e.g., http://localhost:3000)
            environment: Environment (dev/staging/prod)
            api_key: Optional admin token for staging/prod
        """
        self.demo_app_url = demo_app_url.rstrip("/")
        self.environment = environment
        self.headers = {}
        if api_key:
            self.headers["X-Admin-Token"] = api_key
        self.session = requests.Session()

    def health_check(self) -> bool:
        """Check if demo app is healthy."""
        try:
            response = self.session.get(
                f"{self.demo_app_url}/health", timeout=5, headers=self.headers
            )
            logger.info(f"Health check: {response.status_code}")
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    def inject_latency(self, latency_ms: int = 3000) -> bool:
        """Inject latency into demo app.

        Args:
            latency_ms: Milliseconds of latency to inject

        Returns:
            True if successful
        """
        try:
            response = self.session.post(
                f"{self.demo_app_url}/inject/latency",
                json={"ms": latency_ms},
                timeout=5,
                headers=self.headers,
            )
            if response.status_code == 200:
                logger.info(f"✓ Injected {latency_ms}ms latency")
                return True
            else:
                logger.error(f"Failed to inject latency: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"Latency injection failed: {e}")
            return False

    def inject_errors(self, error_rate: float = 0.5) -> bool:
        """Inject error rate into demo app.

        Args:
            error_rate: Error rate (0.0 to 1.0)

        Returns:
            True if successful
        """
        if not 0 <= error_rate <= 1:
            logger.error("Error rate must be between 0.0 and 1.0")
            return False

        try:
            response = self.session.post(
                f"{self.demo_app_url}/inject/errors",
                json={"rate": error_rate},
                timeout=5,
                headers=self.headers,
            )
            if response.status_code == 200:
                logger.info(f"✓ Injected {error_rate*100}% error rate")
                return True
            else:
                logger.error(f"Failed to inject errors: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"Error injection failed: {e}")
            return False

    def inject_memory_pressure(self) -> bool:
        """Inject memory leak into demo app.

        Returns:
            True if successful
        """
        try:
            response = self.session.post(
                f"{self.demo_app_url}/inject/memory",
                json={},
                timeout=5,
                headers=self.headers,
            )
            if response.status_code == 200:
                logger.info("✓ Memory leak injection started")
                return True
            else:
                logger.error(f"Failed to inject memory leak: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"Memory injection failed: {e}")
            return False

    def reset_faults(self) -> bool:
        """Reset all injected faults.

        Returns:
            True if successful
        """
        try:
            response = self.session.post(
                f"{self.demo_app_url}/reset",
                json={},
                timeout=5,
                headers=self.headers,
            )
            if response.status_code == 200:
                logger.info("✓ All faults reset")
                return True
            else:
                logger.error(f"Failed to reset faults: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"Reset failed: {e}")
            return False

    def run_scenario_latency(self, duration_seconds: int = 60) -> dict:
        """Scenario: Inject latency and measure agent response time.

        Args:
            duration_seconds: How long to maintain the fault

        Returns:
            Results dictionary
        """
        results = {"scenario": "latency", "duration_seconds": duration_seconds}

        try:
            logger.info(f"\n📊 Latency Scenario ({duration_seconds}s)")
            logger.info("=" * 50)

            # Inject latency
            if not self.inject_latency(3000):
                return results

            # Monitor for duration
            start_time = time.time()
            success_count = 0
            failure_count = 0
            response_times = []

            while time.time() - start_time < duration_seconds:
                try:
                    t0 = time.time()
                    response = self.session.get(
                        f"{self.demo_app_url}/checkout", timeout=30
                    )
                    elapsed = (time.time() - t0) * 1000

                    response_times.append(elapsed)
                    if response.status_code == 200:
                        success_count += 1
                    else:
                        failure_count += 1

                    logger.info(f"Request: {elapsed:.0f}ms ({response.status_code})")
                except Exception as e:
                    logger.warning(f"Request failed: {e}")
                    failure_count += 1

                time.sleep(5)

            # Calculate stats
            if response_times:
                results["avg_response_ms"] = sum(response_times) / len(response_times)
                results["max_response_ms"] = max(response_times)
                results["min_response_ms"] = min(response_times)
                results["success_count"] = success_count
                results["failure_count"] = failure_count

            # Reset
            self.reset_faults()

            logger.info(f"Results: {json.dumps(results, indent=2)}")
            return results

        except Exception as e:
            logger.error(f"Scenario failed: {e}")
            self.reset_faults()
            return results

    def run_scenario_errors(self, duration_seconds: int = 60) -> dict:
        """Scenario: Inject errors and measure system recovery.

        Args:
            duration_seconds: How long to maintain the fault

        Returns:
            Results dictionary
        """
        results = {"scenario": "errors", "duration_seconds": duration_seconds}

        try:
            logger.info(f"\n📊 Error Injection Scenario ({duration_seconds}s)")
            logger.info("=" * 50)

            # Inject 50% errors
            if not self.inject_errors(0.5):
                return results

            # Monitor for duration
            start_time = time.time()
            success_count = 0
            error_count = 0

            while time.time() - start_time < duration_seconds:
                try:
                    response = self.session.get(
                        f"{self.demo_app_url}/products", timeout=10
                    )
                    if response.status_code == 200:
                        success_count += 1
                    else:
                        error_count += 1

                    logger.info(f"Response: {response.status_code}")
                except Exception as e:
                    logger.warning(f"Request failed: {e}")
                    error_count += 1

                time.sleep(2)

            results["success_count"] = success_count
            results["error_count"] = error_count
            results["error_rate"] = (
                error_count / (success_count + error_count)
                if (success_count + error_count) > 0
                else 0
            )

            # Reset
            self.reset_faults()

            logger.info(f"Results: {json.dumps(results, indent=2)}")
            return results

        except Exception as e:
            logger.error(f"Scenario failed: {e}")
            self.reset_faults()
            return results


def main():
    """Run chaos engineering tests."""
    parser = argparse.ArgumentParser(description="Chaos Engineering Tests")
    parser.add_argument(
        "--demo-app-url",
        default="http://localhost:3000",
        help="Demo app URL",
    )
    parser.add_argument(
        "--environment",
        choices=["dev", "staging", "prod"],
        default="dev",
        help="Environment (dev allows free fault injection, staging/prod require token)",
    )
    parser.add_argument(
        "--admin-token",
        help="Admin token for staging/prod (required for staging/prod)",
    )
    parser.add_argument(
        "--scenario",
        choices=["latency", "errors", "all"],
        default="all",
        help="Chaos scenario to run",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="Duration of each scenario in seconds",
    )

    args = parser.parse_args()

    chaos = ChaosTest(args.demo_app_url, args.environment, args.admin_token)

    # Check health
    if not chaos.health_check():
        logger.error("Demo app is not healthy. Aborting.")
        return

    results = {}

    try:
        if args.scenario in ["latency", "all"]:
            results["latency"] = chaos.run_scenario_latency(args.duration)

        if args.scenario in ["errors", "all"]:
            results["errors"] = chaos.run_scenario_errors(args.duration)

        logger.info(f"\n✅ Chaos tests completed")
        logger.info(f"Overall results: {json.dumps(results, indent=2)}")

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        chaos.reset_faults()


if __name__ == "__main__":
    main()
