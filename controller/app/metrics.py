from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class MetricsRecorder:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.http_requests_total = Counter(
            "auto_browser_http_requests_total",
            "HTTP requests handled by Auto Browser",
            labelnames=("method", "path", "status_code"),
            registry=self.registry,
        )
        self.http_request_duration_seconds = Histogram(
            "auto_browser_http_request_duration_seconds",
            "HTTP request latency for Auto Browser",
            labelnames=("method", "path"),
            registry=self.registry,
        )
        self.active_sessions = Gauge(
            "auto_browser_active_sessions",
            "Currently active browser sessions",
            registry=self.registry,
        )

    def record_http_request(self, *, method: str, path: str, status_code: int, duration_seconds: float) -> None:
        labels = {"method": method, "path": path}
        self.http_requests_total.labels(status_code=str(status_code), **labels).inc()
        self.http_request_duration_seconds.labels(**labels).observe(duration_seconds)

    def set_active_sessions(self, count: int) -> None:
        self.active_sessions.set(count)

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
