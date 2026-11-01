"""Tiny fake of GET /api/states for manual dry-runs.

    python3 tests/fake_ha.py 8123 &
    HA_API_URL=http://127.0.0.1:8123/api SUPERVISOR_TOKEN=x \
        python3 app/collector.py --dry-run --once \
        --options tests/options.fake.json
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

def s(eid, state, **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs}

STATES = [
    s("sensor.fake_a", "20.4"),
    s("sensor.fake_b", "unavailable"),
    s("sensor.fake_c", "48"),
    s("sensor.fake_unknown", "unknown"),
    s("sensor.fake_power", "812.5"),
    s("weather.fake_w", "partlycloudy", temperature=6.0, wind_speed=14.4),
    s("binary_sensor.fake_pump", "on"),
    s("switch.fake_relay", "off"),
    s("input_select.fake_mode", "Comfort, day"),
]

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/states" and self.headers.get("Authorization", "").startswith("Bearer "):
            body = json.dumps(STATES).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(401)
            self.end_headers()

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8123), H).serve_forever()
