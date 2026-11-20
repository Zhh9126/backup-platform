"""Read-only authenticated preview checks, plus fingerprint input validation."""
import json
from pathlib import Path
import requests

base = "http://192.168.109.128:8080"
username, password = Path("instance/initial_admin_password.txt").read_text().strip().split(" / ", 1)
session = requests.Session()
session.trust_env = False
response = session.post(base + "/login", json={"username": username, "password": password}, timeout=30)
assert response.status_code == 200 and response.json().get("ok"), response.status_code
results = {"login": response.status_code}
for route in ("/", "/storage", "/agentless", "/api/chunk-store/stats", "/api/chunk-store/manifests", "/api/dedup/stats"):
    response = session.get(base + route, timeout=30)
    assert response.status_code == 200, (route, response.status_code)
    if "/api/" in route:
        assert response.json().get("ok"), (route, response.text[:300])
    results[route] = response.status_code
response = session.post(base + "/api/chunk-store/have", json={"fps": ["../../invalid"]}, timeout=30)
assert response.status_code == 400, response.status_code
results["invalid_fingerprint"] = response.status_code
Path("build/storage-qa/http-smoke.json").write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))
