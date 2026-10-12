import json
import unittest
from pathlib import Path

import app


class ValidateServiceTests(unittest.TestCase):
    def test_accepts_private_ipv4_subdomain(self):
        self.assertEqual(
            app.validate_service("api-v2.icc.com", "10.8.66.235", 8084),
            ("api-v2.icc.com", "10.8.66.235", 8084),
        )

    def test_normalizes_domain_case(self):
        self.assertEqual(
            app.validate_service("API.ICC.COM", "192.168.1.10", 443),
            ("api.icc.com", "192.168.1.10", 443),
        )

    def test_rejects_apex_or_external_domain(self):
        for domain in ("icc.com", "service.example.com", "bad_name.icc.com"):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                app.validate_service(domain, "10.8.66.235", 8080)

    def test_rejects_public_and_non_ipv4_upstreams(self):
        for address in ("8.8.8.8", "127.0.0.1", "169.254.1.2", "::1"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                app.validate_service("api.icc.com", address, 8080)

    def test_rejects_invalid_ports(self):
        for port in (0, 65536, True, "8080"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                app.validate_service("api.icc.com", "10.8.66.235", port)


class BuildRouteTests(unittest.TestCase):
    def test_creates_host_route_to_configured_upstream(self):
        route = app.build_route({
            "domain": "api.icc.com",
            "upstream_ip": "10.8.66.235",
            "upstream_port": 8084,
        })
        self.assertEqual(route["match"], [{"host": ["api.icc.com"]}])
        self.assertEqual(
            route["handle"][0]["upstreams"],
            [{"dial": "10.8.66.235:8084"}],
        )
        self.assertTrue(route["terminal"])


class InitialServicesTests(unittest.TestCase):
    def test_keeps_existing_three_routes_as_initial_state(self):
        initial_services = json.loads(
            Path(__file__).with_name("initial-services.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [(item["domain"], item["upstream_port"]) for item in initial_services],
            [
                ("servicio1.icc.com", 8081),
                ("servicio2.icc.com", 8082),
                ("servicio3.icc.com", 8083),
            ],
        )


if __name__ == "__main__":
    unittest.main()
