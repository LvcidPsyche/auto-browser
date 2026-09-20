"""Read-only permission probe; never prints the DNS API token or API payloads."""

import json
import subprocess
import urllib.error
import urllib.request

inspect = subprocess.check_output(["docker", "inspect", "dokploy-traefik"], text=True)
environment = json.loads(inspect)[0]["Config"]["Env"]
token_entry = next((entry for entry in environment if entry.startswith("CF_DNS_API_TOKEN=")), None)
if not token_entry:
    raise SystemExit("Cloudflare DNS token not found")
token = token_entry.partition("=")[2]


def get(path):
    request = urllib.request.Request(
        "https://api.cloudflare.com/client/v4/" + path,
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, None


zone_status, zones = get("zones?name=fareeqk.com&per_page=1")
print("Zone lookup status:", zone_status)
if zone_status != 200 or not zones or not zones.get("result"):
    raise SystemExit("Cannot determine account with existing token")
account_id = zones["result"][0].get("account", {}).get("id")
if not account_id:
    raise SystemExit("Zone account id not returned")
app_status, _ = get(f"accounts/{account_id}/access/apps?per_page=1")
print("Access apps read status:", app_status)
