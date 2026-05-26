#!/usr/bin/env python3
"""Load testing script for SiteMedic using Locust.

Usage:
    # Web UI
    locust -f scripts/load_test.py --host=https://sitemedic-agent-dev.run.app

    # Headless (batch)
    locust -f scripts/load_test.py \
      --host=https://sitemedic-agent-dev.run.app \
      --users=100 \
      --spawn-rate=10 \
      --run-time=5m \
      --headless

    # With custom report
    locust -f scripts/load_test.py \
      --host=https://sitemedic-agent-dev.run.app \
      --csv=results \
      --html=report.html
"""

import os
import random
from locust import HttpUser, task, between, events, tag
from locust.contrib.fasthttp import FastHttpUser


class AgentUser(FastHttpUser):
    """User that performs typical agent operations."""

    wait_time = between(1, 5)  # Wait 1-5 seconds between requests

    def on_start(self):
        """Called when a simulated user starts."""
        self.api_key = os.environ.get("AGENT_API_KEY", "test-key")
        self.headers = {"X-API-Key": self.api_key}

    @task(1)
    @tag("health")
    def health_check(self):
        """Health endpoint (1 request per cycle)."""
        with self.client.get("/health", catch_response=True) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Health check failed: {response.status_code}")

    @task(2)
    @tag("list")
    def list_incidents(self):
        """List incidents (2 requests per cycle)."""
        with self.client.get(
            "/api/incidents", headers=self.headers, catch_response=True
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"List incidents failed: {response.status_code}")

    @task(2)
    @tag("list")
    def list_predictions(self):
        """List predictions (2 requests per cycle)."""
        with self.client.get(
            "/api/predictions", headers=self.headers, catch_response=True
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"List predictions failed: {response.status_code}")

    @task(1)
    @tag("details")
    def get_incident(self):
        """Get specific incident (1 request per cycle)."""
        incident_id = f"incident-{random.randint(1, 100)}"
        with self.client.get(
            f"/api/incidents/{incident_id}",
            headers=self.headers,
            catch_response=True,
        ) as response:
            if response.status_code in [200, 404]:  # 404 is expected for non-existent
                response.success()
            else:
                response.failure(f"Get incident failed: {response.status_code}")

    @task(1)
    @tag("analytics")
    def get_analytics(self):
        """Get analytics data (1 request per cycle)."""
        with self.client.get(
            "/api/analytics?window=30d", headers=self.headers, catch_response=True
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Get analytics failed: {response.status_code}")

    @task(1)
    @tag("audit")
    def list_audit_events(self):
        """List audit events (1 request per cycle)."""
        with self.client.get(
            "/api/audit?limit=50", headers=self.headers, catch_response=True
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"List audit failed: {response.status_code}")

    @task(1)
    @tag("health-detail")
    def system_health(self):
        """System health details (1 request per cycle)."""
        with self.client.get(
            "/api/system-health", headers=self.headers, catch_response=True
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"System health failed: {response.status_code}")


class FrontendUser(FastHttpUser):
    """User that performs typical frontend operations."""

    wait_time = between(2, 8)  # Frontend users wait longer

    @task(3)
    @tag("frontend")
    def view_dashboard(self):
        """View dashboard homepage (3 requests per cycle)."""
        with self.client.get("/", catch_response=True) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Dashboard failed: {response.status_code}")

    @task(2)
    @tag("frontend")
    def view_incidents(self):
        """View incidents page (2 requests per cycle)."""
        with self.client.get("/api/incidents", catch_response=True) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Incidents page failed: {response.status_code}")

    @task(1)
    @tag("frontend")
    def view_analytics(self):
        """View analytics page (1 request per cycle)."""
        with self.client.get("/analytics", catch_response=True) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Analytics page failed: {response.status_code}")


# ────────────────────────────────────────────────────────────────────────────
# Event Handlers (for reporting)
# ────────────────────────────────────────────────────────────────────────────


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Called when load test starts."""
    print(f"\n🔥 Load test started: {environment.host}")
    print(f"   Target: {environment.host}")
    print(f"   Users: {environment.runner.target_user_count}")
    print(f"   Spawn rate: {environment.runner.spawn_rate}/sec\n")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Called when load test stops."""
    print("\n✅ Load test completed")
    print(f"\nStatistics:")
    for key, value in environment.stats.total.to_dict().items():
        if key not in ["_name", "response_times", "num_requests", "num_failures"]:
            print(f"  {key}: {value}")


@events.request.add_listener
def on_request(request_type, name, response_time, response_length, exception, **kwargs):
    """Log slow requests."""
    if response_time > 5000:  # Log requests taking >5 seconds
        print(f"⚠️  SLOW: {request_type} {name} took {response_time}ms")
    if exception:
        print(f"❌ ERROR: {request_type} {name} - {exception}")


# ────────────────────────────────────────────────────────────────────────────
# Test Profiles (for quick preset configurations)
# ────────────────────────────────────────────────────────────────────────────


class SmallLoadProfile:
    """Small load: 10 users, 1 user/sec, 5 minutes."""

    wait_time = between(1, 5)
    weight = 1


class MediumLoadProfile:
    """Medium load: 50 users, 5 users/sec, 10 minutes."""

    wait_time = between(1, 3)
    weight = 2


class LargeLoadProfile:
    """Large load: 200 users, 10 users/sec, 20 minutes (stress test)."""

    wait_time = between(0.5, 2)
    weight = 5
