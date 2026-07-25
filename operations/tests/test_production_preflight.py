from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase
from django.urls import reverse


class HealthcheckViewTests(TestCase):
    def test_healthcheck_returns_minimal_success_response(self):
        response = self.client.get(reverse("healthcheck"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    @patch("operations.views.health.connection.cursor", side_effect=OperationalError)
    def test_healthcheck_returns_service_unavailable_when_database_is_down(self, _cursor):
        response = self.client.get(reverse("healthcheck"))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})

    def test_healthcheck_rejects_non_get_request(self):
        self.assertEqual(self.client.post(reverse("healthcheck")).status_code, 405)
