from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from requests import Response

from core import data
from gspread.exceptions import APIError


class DataCacheTests(unittest.TestCase):
    def test_valid_ttl_cache_does_not_contact_google(self) -> None:
        with (
            patch.object(data, "_last_pull", 1_000.0),
            patch.object(data.time, "time", return_value=1_100.0),
            patch.object(data, "_sh", side_effect=AssertionError("unexpected API read")),
            patch.dict(data.os.environ, {}, clear=False),
        ):
            data.os.environ.pop("NEUROSHIFT_NOCACHE", None)
            self.assertFalse(data._should_refresh())

    def test_expired_ttl_requests_refresh_without_contacting_google(self) -> None:
        with (
            patch.object(data, "_last_pull", 1_000.0),
            patch.object(data.time, "time", return_value=1_301.0),
            patch.object(data, "_sh", side_effect=AssertionError("unexpected API read")),
        ):
            self.assertTrue(data._should_refresh())

    def test_quota_error_is_retried_with_backoff(self) -> None:
        response = Response()
        response.status_code = 429
        response._content = json.dumps(
            {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}
        ).encode()
        op = Mock(side_effect=[APIError(response), "ok"])

        with patch.object(data.time, "sleep") as sleep:
            self.assertEqual(data._retry(op, attempts=2), "ok")

        sleep.assert_called_once_with(2.0)


if __name__ == "__main__":
    unittest.main()
