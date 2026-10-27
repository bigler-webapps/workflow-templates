"""Regression coverage for app-ci's host-network PostgreSQL endpoint."""

from __future__ import annotations

import os
import shlex
import socket
import unittest
from unittest.mock import patch
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "app-ci.yml"
WORKFLOW = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
BACKEND = WORKFLOW["jobs"]["backend"]


def _step(name: str) -> dict:
    return next(step for step in BACKEND["steps"] if step.get("name") == name)


def _environment_assignments(run: str) -> dict[str, list[str]]:
    assignments: dict[str, list[str]] = {}
    for line in run.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("-e "):
            continue
        tokens = shlex.split(stripped.removesuffix("\\").rstrip(), posix=True)
        if len(tokens) != 2 or tokens[0] != "-e" or "=" not in tokens[1]:
            raise AssertionError(f"Could not parse active docker environment argument: {line!r}")
        name, value = tokens[1].split("=", 1)
        assignments.setdefault(name, []).append(value)
    return assignments


class AppCiDatabaseEndpointTests(unittest.TestCase):
    def test_service_port_is_published_dynamically(self):
        self.assertEqual(BACKEND["services"]["postgres"]["ports"], [5432])

    def test_backend_steps_use_numeric_loopback_and_the_published_port(self):
        for name in ("Run pytest", "Check for missing migrations"):
            with self.subTest(step=name):
                run = _step(name)["run"]
                self.assertIn("docker run --rm --network host", run)
                env = _environment_assignments(run)
                expected = {
                    "TEST_DB_HOST": "127.0.0.1",
                    "DB_HOST": "127.0.0.1",
                    "DB_PORT": "${{ job.services.postgres.ports['5432'] }}",
                    "DJANGO_SETTINGS_MODULE": "${{ inputs.django-settings-module }}",
                    "DJANGO_SECRET_KEY": "ci-dummy-secret",
                    "DB_NAME": "test",
                    "DB_USER": "test",
                    "DB_PASSWORD": "test",
                    "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1",
                }
                for variable, value in expected.items():
                    self.assertEqual(env.get(variable), [value], variable)

    @unittest.skipUnless(
        os.environ.get("RUN_APP_CI_DB_ENDPOINT_INTEGRATION") == "1",
        "set RUN_APP_CI_DB_ENDPOINT_INTEGRATION=1 with APP_CI_DB_PORT to exercise a disposable PostgreSQL service",
    )
    def test_numeric_endpoint_connects_without_localhost_resolution(self):
        """Opt-in check against the job's disposable PostgreSQL service.

        The port is the selected dynamic published port. Credentials and the
        database name are fixed synthetic CI values; app settings and .env files
        are deliberately not consulted.
        """
        import psycopg

        port = int(os.environ["APP_CI_DB_PORT"])
        self.assertGreater(port, 0)

        original_getaddrinfo = socket.getaddrinfo

        def reject_localhost(host, *args, **kwargs):
            if host == "localhost":
                raise socket.gaierror("localhost resolution disabled by regression test")
            return original_getaddrinfo(host, *args, **kwargs)

        with patch("socket.getaddrinfo", side_effect=reject_localhost):
            with self.assertRaises(socket.gaierror):
                socket.getaddrinfo("localhost", port)
            with psycopg.connect(
                host="127.0.0.1",
                port=port,
                dbname="test",
                user="test",
                password="test",
                connect_timeout=5,
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT current_database()")
                    self.assertEqual(cursor.fetchone()[0], "test")


if __name__ == "__main__":
    unittest.main()
