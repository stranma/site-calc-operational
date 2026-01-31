"""Operational Client for Site-Calc API."""

import time
import warnings
from typing import Optional, Any
import httpx

from site_calc_operational import __version__


class OperationalClient:
    """Client for Site-Calc operational optimization API.

    This client is specifically for day-ahead bidding and short-term dispatch
    optimization with ancillary services. It:
    - Supports 15-minute and 1-hour resolutions
    - Maximum 296 intervals (~3 days)
    - Supports ancillary services (aFRR, mFRR)
    - Has access to /optimal-bidding and /device-planning endpoints

    Example:
        >>> client = OperationalClient(
        ...     base_url="https://api.site-calc.example.com",
        ...     api_key="op_your_key_here"
        ... )
        >>> job = client.create_optimal_bidding_job(request)
        >>> result = client.wait_for_completion(job.job_id, timeout=600)
        >>> print(f"Profit: {result.summary.expected_profit}")
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 300.0,
        max_retries: int = 3,
    ):
        """Initialize the operational client.

        Args:
            base_url: Base URL of the API (e.g., "https://api.site-calc.example.com")
            api_key: API key with 'op_' prefix (operational client)
            timeout: Default request timeout in seconds (default: 5 minutes)
            max_retries: Maximum number of retry attempts for failed requests

        Raises:
            ValueError: If API key doesn't start with 'op_'
        """
        if not api_key.startswith("op_"):
            raise ValueError("API key must start with 'op_' for operational client")

        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_intervals = 296

        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
        self._version_checked = False

    def __enter__(self) -> "OperationalClient":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def _validate_server_version(self) -> None:
        """Check server API version compatibility and warn if mismatched.

        Compares client MAJOR.MINOR with server api_version.
        Only runs once per client instance.
        """
        if self._version_checked:
            return

        self._version_checked = True
        client_api_version = ".".join(__version__.split(".")[:2])

        try:
            response = self._client.get("/health")
            if response.status_code == 200:
                health = response.json()
                server_api_version = health.get("api_version")
                if server_api_version and client_api_version != server_api_version:
                    warnings.warn(
                        f"Client version {__version__} (API {client_api_version}) may not be compatible "
                        f"with server API {server_api_version}. Consider upgrading.",
                        UserWarning,
                        stacklevel=3,
                    )
        except Exception:
            pass

    def _handle_error(self, response: httpx.Response) -> None:
        """Handle API error responses.

        Args:
            response: HTTP response with error status

        Raises:
            Exception with appropriate error message
        """
        try:
            error_data = response.json()
            error = error_data.get("error", {})
            message = error.get("message", "Unknown error")
        except Exception:
            message = response.text or f"HTTP {response.status_code}"

        raise Exception(f"API error ({response.status_code}): {message}")

    def _request_with_retry(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> httpx.Response:
        """Make HTTP request with retry logic.

        Args:
            method: HTTP method (GET, POST, DELETE)
            path: API path
            **kwargs: Additional arguments for httpx

        Returns:
            HTTP response

        Raises:
            Various exceptions based on response status
        """
        self._validate_server_version()
        last_exception = None

        for attempt in range(self.max_retries):
            try:
                response = self._client.request(method, path, **kwargs)

                if response.status_code < 400:
                    return response

                if 400 <= response.status_code < 500 and response.status_code not in [408, 429]:
                    self._handle_error(response)

                if attempt < self.max_retries - 1:
                    wait_time = 2**attempt
                    time.sleep(wait_time)
                    continue

                self._handle_error(response)

            except httpx.TimeoutException:
                last_exception = Exception(f"Request timeout after {self.timeout}s")
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise last_exception
            except httpx.RequestError as e:
                last_exception = Exception(f"Request failed: {str(e)}")
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise last_exception

        if last_exception:
            raise last_exception
        raise Exception("Request failed after retries")

    def create_optimal_bidding_job(self, request: Any) -> Any:
        """Create an optimal bidding job.

        Args:
            request: OptimalBiddingRequest object

        Returns:
            Job object with job_id and initial status

        Note:
            This method requires the models module to be implemented.
        """
        payload = request.model_dump_for_api() if hasattr(request, "model_dump_for_api") else request

        response = self._request_with_retry(
            "POST",
            "/api/v1/jobs/optimal-bidding",
            json=payload,
        )

        return response.json()

    def create_device_planning_job(self, request: Any) -> Any:
        """Create a device planning job.

        Args:
            request: DevicePlanningRequest object

        Returns:
            Job object with job_id and initial status

        Note:
            This method requires the models module to be implemented.
        """
        payload = request.model_dump_for_api() if hasattr(request, "model_dump_for_api") else request

        response = self._request_with_retry(
            "POST",
            "/api/v1/jobs/device-planning",
            json=payload,
        )

        return response.json()

    def get_job_status(self, job_id: str) -> Any:
        """Get current job status.

        Args:
            job_id: Job identifier

        Returns:
            Job status information
        """
        response = self._request_with_retry(
            "GET",
            f"/api/v1/jobs/{job_id}",
        )

        return response.json()

    def get_job_result(self, job_id: str) -> Any:
        """Get job result (must be completed).

        Args:
            job_id: Job identifier

        Returns:
            Complete optimization result
        """
        response = self._request_with_retry(
            "GET",
            f"/api/v1/jobs/{job_id}/result",
        )

        return response.json()

    def cancel_job(self, job_id: str) -> Any:
        """Cancel a running job.

        Args:
            job_id: Job identifier

        Returns:
            Job object with cancelled status
        """
        response = self._request_with_retry(
            "DELETE",
            f"/api/v1/jobs/{job_id}",
        )

        return response.json()

    def wait_for_completion(
        self,
        job_id: str,
        poll_interval: float = 5,
        timeout: Optional[float] = 600,
    ) -> Any:
        """Wait for job to complete and return result.

        Polls the job status at regular intervals until completion or timeout.

        Args:
            job_id: Job identifier
            poll_interval: Seconds between status checks (default: 5s)
            timeout: Maximum wait time in seconds (default: 10 minutes, None=unlimited)

        Returns:
            Complete optimization result

        Raises:
            TimeoutError: If timeout is exceeded
            Exception: If job fails
        """
        start_time = time.time()

        while True:
            job = self.get_job_status(job_id)
            status = job.get("status") if isinstance(job, dict) else getattr(job, "status", None)

            if status == "completed":
                return self.get_job_result(job_id)
            elif status == "failed":
                error = job.get("error", {}) if isinstance(job, dict) else {}
                error_msg = error.get("message", "Unknown error") if isinstance(error, dict) else str(error)
                raise Exception(f"Optimization failed: {error_msg}")
            elif status == "cancelled":
                raise Exception("Job was cancelled")

            if timeout is not None:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    raise TimeoutError(f"Job did not complete within {timeout}s")

            time.sleep(poll_interval)
