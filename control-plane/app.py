import hmac
import ipaddress
import json
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


API_TOKEN = os.environ.get("API_TOKEN", "")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/var/lib/control-plane/services.sqlite3")
CADDY_ADMIN_URL = os.environ.get("CADDY_ADMIN_URL", "http://caddy:2019").rstrip("/")
DOMAIN_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+icc\.com\Z"
)
SYNC_LOCK = threading.Lock()
SYNC_EVENT = threading.Event()


@contextmanager
def connect_db():
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def validate_service(domain, upstream_ip, upstream_port):
    if not isinstance(domain, str) or not DOMAIN_PATTERN.fullmatch(domain.lower()):
        raise ValueError("domain debe ser un subdominio DNS válido de icc.com")
    domain = domain.lower()

    if not isinstance(upstream_ip, str):
        raise ValueError("upstream_ip debe ser una dirección IPv4 privada")
    try:
        parsed_ip = ipaddress.ip_address(upstream_ip)
    except ValueError as error:
        raise ValueError("upstream_ip debe ser una dirección IPv4 privada") from error
    if (
        parsed_ip.version != 4
        or not parsed_ip.is_private
        or parsed_ip.is_loopback
        or parsed_ip.is_link_local
        or parsed_ip.is_multicast
        or parsed_ip.is_unspecified
    ):
        raise ValueError("upstream_ip debe ser una dirección IPv4 privada enrutable")

    if isinstance(upstream_port, bool) or not isinstance(upstream_port, int):
        raise ValueError("upstream_port debe ser un entero entre 1 y 65535")
    if not 1 <= upstream_port <= 65535:
        raise ValueError("upstream_port debe ser un entero entre 1 y 65535")

    return domain, str(parsed_ip), upstream_port


def service_rows():
    with connect_db() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT domain, upstream_ip, upstream_port FROM services ORDER BY domain"
        )]


def caddy_request(method, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{CADDY_ADMIN_URL}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(f"No se pudo actualizar Caddy: {error}") from error


def build_route(service):
    return {
        "match": [{"host": [service["domain"]]}],
        "handle": [{
            "handler": "reverse_proxy",
            "upstreams": [{
                "dial": f'{service["upstream_ip"]}:{service["upstream_port"]}'
            }],
        }],
        "terminal": True,
    }


def synchronize_caddy():
    with SYNC_LOCK:
        services = service_rows()
        config_path = "/config/"
        request = urllib.request.Request(f"{CADDY_ADMIN_URL}{config_path}", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                config = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(f"No se pudo leer la configuración de Caddy: {error}") from error

        try:
            server = config["apps"]["http"]["servers"]["srv0"]
            routes = server.setdefault("routes", [])
        except (KeyError, TypeError) as error:
            raise RuntimeError("Caddy no tiene el servidor HTTP srv0 esperado") from error

        retained_routes = []
        for route in routes:
            hosts = [
                host.lower()
                for matcher in route.get("match", [])
                for host in matcher.get("host", [])
                if isinstance(host, str)
            ]
            if not any(host.endswith(".icc.com") for host in hosts):
                retained_routes.append(route)

        server["routes"] = [build_route(service) for service in services] + retained_routes
        caddy_request("POST", "/load", config)


def initialize_database():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    with connect_db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS services (
                domain TEXT PRIMARY KEY,
                upstream_ip TEXT NOT NULL,
                upstream_port INTEGER NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS control_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        initialized = connection.execute(
            "SELECT 1 FROM control_state WHERE key = 'initial_services_loaded'"
        ).fetchone()
        if initialized is None:
            with open("/app/initial-services.json", encoding="utf-8") as initial_file:
                initial_services = json.load(initial_file)
            for service in initial_services:
                domain, upstream_ip, upstream_port = validate_service(
                    service["domain"], service["upstream_ip"], service["upstream_port"]
                )
                connection.execute(
                    "INSERT INTO services (domain, upstream_ip, upstream_port) VALUES (?, ?, ?)",
                    (domain, upstream_ip, upstream_port),
                )
            connection.execute(
                "INSERT INTO control_state (key, value) VALUES ('initial_services_loaded', 'true')"
            )


def synchronize_with_retry():
    while True:
        try:
            synchronize_caddy()
        except RuntimeError as error:
            print(f"Sincronización pendiente: {error}", flush=True)
        SYNC_EVENT.wait(5)
        SYNC_EVENT.clear()


class ControlPlaneHandler(BaseHTTPRequestHandler):
    server_version = "DynamicProxyControl/1.0"

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}", flush=True)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        token = self.headers.get("Authorization", "")
        expected = f"Bearer {API_TOKEN}"
        if not API_TOKEN or not hmac.compare_digest(token, expected):
            self.send_json(401, {"error": "Se requiere un token Bearer válido"})
            return False
        return True

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 16_384:
                raise ValueError
            return json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            raise ValueError("El cuerpo debe ser un objeto JSON válido de hasta 16 KB")

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path == "/health":
            self.send_json(200, {"status": "ok"})
            return
        if parsed_url.path == "/certificates/authorize":
            domain = urllib.parse.parse_qs(parsed_url.query).get("domain", [""])[0].lower()
            allowed = any(service["domain"] == domain for service in service_rows())
            self.send_response(200 if allowed else 403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed_url.path != "/services":
            self.send_json(404, {"error": "Ruta no encontrada"})
            return
        if not self.authorized():
            return
        self.send_json(200, {"services": service_rows()})

    def do_POST(self):
        if self.path != "/services":
            self.send_json(404, {"error": "Ruta no encontrada"})
            return
        if not self.authorized():
            return
        try:
            payload = self.read_json()
            if not isinstance(payload, dict):
                raise ValueError("El cuerpo debe ser un objeto JSON")
            domain, upstream_ip, upstream_port = validate_service(
                payload.get("domain"),
                payload.get("upstream_ip"),
                payload.get("upstream_port"),
            )
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return

        with connect_db() as connection:
            connection.execute(
                """INSERT INTO services (domain, upstream_ip, upstream_port)
                   VALUES (?, ?, ?)
                   ON CONFLICT(domain) DO UPDATE SET
                     upstream_ip = excluded.upstream_ip,
                     upstream_port = excluded.upstream_port""",
                (domain, upstream_ip, upstream_port),
            )
        try:
            synchronize_caddy()
        except RuntimeError as error:
            SYNC_EVENT.set()
            self.send_json(503, {
                "error": str(error),
                "saved": True,
                "message": "El servicio quedó guardado y se aplicará al recuperar Caddy",
            })
            return
        self.send_json(201, {
            "service": {
                "domain": domain,
                "upstream_ip": upstream_ip,
                "upstream_port": upstream_port,
            },
            "applied": True,
        })

    def do_DELETE(self):
        if not self.authorized():
            return
        prefix = "/services/"
        if not self.path.startswith(prefix):
            self.send_json(404, {"error": "Ruta no encontrada"})
            return
        domain = urllib.parse.unquote(self.path[len(prefix):]).lower()
        if not DOMAIN_PATTERN.fullmatch(domain):
            self.send_json(400, {"error": "Dominio inválido"})
            return
        with connect_db() as connection:
            result = connection.execute("DELETE FROM services WHERE domain = ?", (domain,))
            deleted = result.rowcount > 0
        if not deleted:
            self.send_json(404, {"error": "El servicio no existe"})
            return
        try:
            synchronize_caddy()
        except RuntimeError as error:
            SYNC_EVENT.set()
            self.send_json(503, {
                "error": str(error),
                "deleted": True,
                "message": "El servicio se quitó del estado deseado y Caddy se sincronizará al recuperarse",
            })
            return
        self.send_json(200, {"deleted": domain, "applied": True})


def main():
    if len(API_TOKEN) < 32:
        raise RuntimeError("API_TOKEN debe tener al menos 32 caracteres")
    initialize_database()
    threading.Thread(target=synchronize_with_retry, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", 8080), ControlPlaneHandler)
    print("Plano de control escuchando en 0.0.0.0:8080", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
