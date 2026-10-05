import sys
import json
import urllib.request
import urllib.parse
import ssl
import base64
import xml.etree.ElementTree as ET


def build_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def get_auth_token(vcd_url, api_token, org, user, password, ctx):
    """
    Attempts to authenticate with VMware Cloud Director via:
    1. OAuth refresh token exchange at /oauth/provider/token
    2. OAuth refresh token exchange at /oauth/tenant/{org}/token
    3. Basic auth session creation at /api/sessions (if user & password available)
    4. Fallback to api_token directly
    """
    # 1. Try OAuth refresh_token at /oauth/provider/token
    if api_token:
        oauth_endpoints = [
            f"{vcd_url}/oauth/provider/token",
        ]
        if org and org.lower() != "system":
            oauth_endpoints.append(f"{vcd_url}/oauth/tenant/{org}/token")

        for endpoint in oauth_endpoints:
            try:
                data = urllib.parse.urlencode({
                    "grant_type": "refresh_token",
                    "refresh_token": api_token
                }).encode("utf-8")
                req = urllib.request.Request(
                    endpoint,
                    data=data,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    access_token = payload.get("access_token")
                    if access_token:
                        return access_token
            except Exception as e:
                sys.stderr.write(f"[DEBUG] OAuth failed at {endpoint}: {e}\n")

    # 2. Try Basic Auth at /api/sessions if credentials provided
    if user and password:
        session_creds = [
            f"{user}@{org}:{password}" if org else f"{user}:{password}",
            f"{user}@System:{password}"
        ]
        for cred in session_creds:
            try:
                b64_auth = base64.b64encode(cred.encode("utf-8")).decode("ascii")
                req = urllib.request.Request(
                    f"{vcd_url}/api/sessions",
                    headers={
                        "Accept": "application/*+json",
                        "Authorization": f"Basic {b64_auth}"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                    tok = (resp.headers.get("X-VMWARE-VCLOUD-ACCESS-TOKEN") or
                           resp.headers.get("x-vcloud-authorization"))
                    if tok:
                        return tok
            except Exception as e:
                sys.stderr.write(f"[DEBUG] Basic auth failed for {cred}: {e}\n")

    # 3. Direct token fallback if provided
    return api_token or None


def parse_snapshots_from_body(raw_body):
    """
    Parses snapshot information from either CloudAPI JSON, legacy API JSON,
    or legacy SnapshotSection XML.
    """
    snapshots_list = []
    text = raw_body.decode("utf-8", errors="replace").strip()

    # Try parsing as JSON first
    try:
        data = json.loads(text)
        # Modern OpenAPI /cloudapi/1.0.0/vm/{urn}/snapshots
        items = data.get("values")
        if items is None and isinstance(data, list):
            items = data

        if items is not None:
            for item in items:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("id", "Snapshot")
                    created = (item.get("dateCreated") or
                               item.get("created") or
                               item.get("createdDate") or "")
                    snapshots_list.append({"name": name, "created": created})
            if snapshots_list:
                return snapshots_list

        # Legacy XML-to-JSON representation
        legacy_snaps = data.get("snapshot") or data.get("Snapshot") or []
        if isinstance(legacy_snaps, dict):
            legacy_snaps = [legacy_snaps]
        for s in legacy_snaps:
            if isinstance(s, dict):
                name = s.get("name") or s.get("@name") or "Snapshot"
                created = (s.get("created") or
                           s.get("@created") or
                           s.get("dateCreated") or "")
                snapshots_list.append({"name": name, "created": created})
        if snapshots_list:
            return snapshots_list
    except Exception:
        pass

    # Try parsing as XML
    try:
        root = ET.fromstring(text)
        for elem in root.iter():
            if elem.tag.endswith("Snapshot") and "Section" not in elem.tag:
                name = (elem.attrib.get("name") or
                        elem.attrib.get("SnapshotName") or "Snapshot")
                created = (elem.attrib.get("created") or
                           elem.attrib.get("createdDate") or
                           elem.attrib.get("dateCreated") or "")
                snapshots_list.append({"name": name, "created": created})
    except Exception:
        pass

    return snapshots_list


def get_snapshots():
    # Read query from stdin (Terraform external data source protocol)
    try:
        raw_input = sys.stdin.read()
        if not raw_input:
            print(json.dumps({}))
            return
        query = json.loads(raw_input)
    except Exception as e:
        sys.stderr.write(f"[ERROR] Failed to read input query: {e}\n")
        print(json.dumps({}))
        return

    vcd_url = query.get("vcd_url", "").rstrip("/")
    api_token = query.get("vcd_api_token", "")
    org = query.get("vcd_org", "")
    user = query.get("vcd_user", "")
    password = query.get("vcd_password", "")

    try:
        vm_ids = json.loads(query.get("vm_ids", "{}"))
    except Exception:
        vm_ids = {}

    if not vcd_url or not vm_ids:
        print(json.dumps({name: "[]" for name in vm_ids}))
        return

    ctx = build_ssl_context()
    token = get_auth_token(vcd_url, api_token, org, user, password, ctx)

    if not token:
        sys.stderr.write("[WARNING] Could not authenticate to vCD; returning empty snapshots list.\n")
        print(json.dumps({name: "[]" for name in vm_ids}))
        return

    headers = {
        "Accept": "application/json, application/*+json;q=0.9, */*;q=0.8",
        "Authorization": f"Bearer {token}",
        "X-VMWARE-VCLOUD-ACCESS-TOKEN": token,
        "x-vcloud-authorization": token
    }

    results = {}
    for name, vm_id in vm_ids.items():
        urn = vm_id if vm_id.startswith("urn:vcloud:vm:") else f"urn:vcloud:vm:{vm_id}"
        uuid = vm_id.split(":")[-1]

        candidate_urls = [
            f"{vcd_url}/cloudapi/1.0.0/vm/{urn}/snapshots",
            f"{vcd_url}/cloudapi/1.0.0/vm/{uuid}/snapshots",
            f"{vcd_url}/api/vApp/vm-{uuid}/snapshotSection",
            f"{vcd_url}/api/vApp/vm-{uuid}"
        ]

        vm_snapshots = []
        for url in candidate_urls:
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                    body = resp.read()
                    snaps = parse_snapshots_from_body(body)
                    if snaps:
                        vm_snapshots = snaps
                        break
            except Exception as e:
                sys.stderr.write(f"[DEBUG] Snapshot check failed for {name} on {url}: {e}\n")

        results[name] = json.dumps(vm_snapshots)

    print(json.dumps(results))


if __name__ == "__main__":
    get_snapshots()
