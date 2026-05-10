import requests, sys

r = requests.get("http://localhost:8000/api/test-trigger", timeout=5)
data = r.json()
print("OK ✓" if data.get("ok") else "FAILED ✗", "—", data.get("msg", data))
sys.exit(0 if data.get("ok") else 1)
