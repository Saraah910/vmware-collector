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


def get_auth_token(vcd_url, api_token, org, user, password, ctx, verbose=False):
    """
    Attempts to authenticate with VMware Cloud Director via:
    1. OAuth refresh token exchange at /oauth/provider/token
    2. OAuth refresh token exchange at /oauth/tenant/{org}/token
    3. Basic auth session creation at /api/sessions (if user & password available)
    4. Fallback to api_token directly
    """
    # Normalize vcd_url: remove trailing slashes and /api if present
    base_vcd_url = vcd_url.rstrip("/")
    if base_vcd_url.endswith("/api"):
        base_vcd_url = base_vcd_url[:-4]

    # 1. Try OAuth refresh_token
    if api_token:
        oauth_endpoints = [
            f"{base_vcd_url}/oauth/provider/token",
        ]
        if org and org.lower() != "system":
            oauth_endpoints.append(f"{base_vcd_url}/oauth/tenant/{org}/token")

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
                with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    access_token = payload.get("access_token")
                    if access_token:
                        if verbose:
                            sys.stderr.write(f"[INFO] Successfully authenticated via OAuth at {endpoint}\n")
                        return access_token
            except Exception as e:
                if verbose:
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
                    f"{base_vcd_url}/api/sessions",
                    headers={
                        "Accept": "application/*+json;version=37.0, application/*+xml;version=37.0, application/json, */*",
                        "Authorization": f"Basic {b64_auth}"
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                    tok = (resp.headers.get("X-VMWARE-VCLOUD-ACCESS-TOKEN") or
                           resp.headers.get("x-vcloud-authorization"))
                    if tok:
                        if verbose:
                            sys.stderr.write(f"[INFO] Successfully authenticated via Basic Auth at /api/sessions\n")
                        return tok
            except Exception as e:
                if verbose:
                    sys.stderr.write(f"[DEBUG] Basic auth failed for {cred}: {e}\n")

    # 3. Direct token fallback if provided
    return api_token or None


def parse_snapshots_from_body(raw_body):
    """
    Parses snapshot information from either CloudAPI JSON, legacy API JSON,
    or legacy SnapshotSection XML.
    """
    snapshots_list = []
    text = raw_body.strip() if isinstance(raw_body, str) else raw_body.decode("utf-8", errors="replace").strip()

    # 1. Try parsing as JSON
    try:
        data = json.loads(text)

        # OpenAPI /cloudapi/1.0.0/vm/{urn}/snapshots -> {"resultTotal": ..., "values": [...]}
        items = data.get("values")
        if items is None and isinstance(data, list):
            items = data
        elif items is None and isinstance(data, dict):
            # Could be {"snapshot": [...]} or {"snapshots": [...]}
            items = data.get("snapshot") or data.get("Snapshot") or data.get("snapshots") or data.get("Snapshots")
            if isinstance(items, dict):
                items = [items]
            # Single snapshot object: {"id": "...", "name": "...", "dateCreated": "..."}
            if items is None and ("dateCreated" in data or "created" in data or "name" in data):
                items = [data]

        if items is not None:
            for item in items:
                if isinstance(item, dict):
                    name = item.get("name") or item.get("SnapshotName") or item.get("id") or "Snapshot"
                    created = (item.get("dateCreated") or
                               item.get("created") or
                               item.get("createdDate") or
                               item.get("timestamp") or "")
                    snapshots_list.append({"name": str(name), "created": str(created)})
            if snapshots_list:
                return snapshots_list
    except Exception:
        pass

    # 2. Try parsing as XML
    try:
        root = ET.fromstring(text)
        for elem in root.iter():
            tag = elem.tag.split("}")[-1].lower()
            if tag == "snapshot":
                name = elem.attrib.get("name") or elem.attrib.get("SnapshotName") or ""
                created = (elem.attrib.get("created") or
                           elem.attrib.get("createdDate") or
                           elem.attrib.get("dateCreated") or
                           elem.attrib.get("timestamp") or "")

                # Inspect child elements for Name / Description / Created
                for child in elem:
                    ctag = child.tag.split("}")[-1].lower()
                    if ctag in ["name", "description", "title"] and child.text and child.text.strip():
                        if not name:
                            name = child.text.strip()
                    if ctag in ["created", "createddate", "datecreated", "timestamp"] and child.text and child.text.strip():
                        if not created:
                            created = child.text.strip()

                if not name:
                    name = "Snapshot"

                snapshots_list.append({"name": str(name), "created": str(created)})

        if snapshots_list:
            return snapshots_list
    except Exception:
        pass

    return snapshots_list


def query_vm_snapshots(vcd_url, headers, ctx, name, vm_id, verbose=False):
    """
    Queries all possible candidate endpoints for a specific VM.
    """
    base_vcd_url = vcd_url.rstrip("/")
    if base_vcd_url.endswith("/api"):
        base_vcd_url = base_vcd_url[:-4]

    urn = vm_id if vm_id.startswith("urn:vcloud:vm:") else f"urn:vcloud:vm:{vm_id}"
    uuid = vm_id.split(":")[-1]

    candidate_urls = [
        # OpenAPI endpoints (both singular and plural)
        f"{base_vcd_url}/cloudapi/1.0.0/vm/{urn}/snapshots",
        f"{base_vcd_url}/cloudapi/1.0.0/vms/{urn}/snapshots",
        f"{base_vcd_url}/cloudapi/1.0.0/vm/{uuid}/snapshots",
        f"{base_vcd_url}/cloudapi/1.0.0/vms/{uuid}/snapshots",
        # Legacy API endpoints
        f"{base_vcd_url}/api/vApp/vm-{uuid}/snapshotSection",
        f"{base_vcd_url}/api/vApp/vm-{uuid}",
        f"{base_vcd_url}/api/vApp/vm-{uuid}/snapshots",
    ]

    for url in candidate_urls:
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                body = resp.read()
                snaps = parse_snapshots_from_body(body)
                if snaps:
                    if verbose:
                        sys.stderr.write(f"[INFO] Found {len(snaps)} snapshot(s) for {name} via {url}\n")
                    return snaps
                elif verbose:
                    sys.stderr.write(f"[DEBUG] {url} responded HTTP {resp.status}, but returned 0 snapshots.\n")
        except urllib.error.HTTPError as he:
            if verbose:
                sys.stderr.write(f"[DEBUG] HTTP {he.code} for {name} on {url}\n")
        except Exception as e:
            if verbose:
                sys.stderr.write(f"[DEBUG] Failed for {name} on {url}: {e}\n")

    return []


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
    verbose = query.get("verbose", False)

    try:
        vm_ids = json.loads(query.get("vm_ids", "{}"))
    except Exception:
        vm_ids = {}

    if not vcd_url or not vm_ids:
        print(json.dumps({name: "[]" for name in vm_ids}))
        return

    ctx = build_ssl_context()
    token = get_auth_token(vcd_url, api_token, org, user, password, ctx, verbose=verbose)

    if not token:
        sys.stderr.write("[WARNING] Could not authenticate to vCD; returning empty snapshots list.\n")
        print(json.dumps({name: "[]" for name in vm_ids}))
        return

    headers = {
        "Accept": "application/*+json;version=37.0, application/*+xml;version=37.0, application/json, */*",
        "Authorization": f"Bearer {token}",
        "X-VMWARE-VCLOUD-ACCESS-TOKEN": token,
        "x-vcloud-authorization": token
    }

    results = {}
    for name, vm_id in vm_ids.items():
        snaps = query_vm_snapshots(vcd_url, headers, ctx, name, vm_id, verbose=verbose)
        results[name] = json.dumps(snaps)

    print(json.dumps(results))


if __name__ == "__main__":
    get_snapshots()
