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
    Authenticates with VMware Cloud Director via:
    1. OAuth refresh token exchange at /oauth/provider/token
    2. OAuth refresh token exchange at /oauth/tenant/{org}/token
    3. Basic auth session creation at /api/sessions
    4. Fallback to api_token directly
    """
    base_vcd_url = vcd_url.rstrip("/")
    if base_vcd_url.endswith("/api"):
        base_vcd_url = base_vcd_url[:-4]

    if api_token:
        oauth_endpoints = [f"{base_vcd_url}/oauth/provider/token"]
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

    return api_token or None


def map_device_type(sub_type, host_resource, media_name):
    """
    Dynamically maps VMware virtual CD-ROM hardware subtype to human-readable label.
    Matches vCenter Web Client display:
    - vmware.cdrom.passthrough / atapi -> 'Host Device'
    - vmware.cdrom.remote_passthrough / remote_atapi -> 'Client Device'
    - vmware.cdrom.iso with media inserted -> media name / ISO file name
    - vmware.cdrom.iso with no media -> 'Datastore ISO file'
    """
    if media_name:
        return media_name

    if host_resource and host_resource.strip():
        # Clean up URN or datastore path
        val = host_resource.strip()
        if "/" in val:
            return val.split("/")[-1]
        return val

    sub = (sub_type or "").lower()
    if "passthrough" in sub or "atapi" in sub or "raw" in sub:
        return "Host Device"
    elif "remote" in sub or "client" in sub:
        return "Client Device"
    elif "iso" in sub:
        return "Datastore ISO file"

    return sub_type if sub_type else "Host Device"


def parse_cd_dvd_from_body(raw_body):
    """
    Dynamically parses CD/DVD hardware items from vCD XML or JSON response.
    Returns empty list if no CD/DVD hardware is present. No hardcoded dummy items.
    """
    devices = []
    text = raw_body.strip() if isinstance(raw_body, str) else raw_body.decode("utf-8", errors="replace").strip()

    # 1. Try parsing JSON
    try:
        data = json.loads(text)
        links = data.get("link", []) or data.get("links", [])
        if isinstance(links, dict):
            links = [links]
        eject_link = next((l for l in links if isinstance(l, dict) and "media:ejectMedia" in l.get("rel", "")), None)

        hw_section = data.get("virtualHardwareSection", {}) or data.get("VirtualHardwareSection", {})
        items = hw_section.get("item", []) or hw_section.get("Item", [])
        if isinstance(items, dict):
            items = [items]

        for item in items:
            if not isinstance(item, dict):
                continue
            res_type = str(item.get("resourceType") or item.get("ResourceType") or "")
            element_name = str(item.get("elementName") or item.get("ElementName") or "")
            sub_type = str(item.get("resourceSubType") or item.get("ResourceSubType") or "")
            host_resource = str(item.get("hostResource") or item.get("HostResource") or "")
            conn_val = item.get("connected") or item.get("Connected") or item.get("connection") or item.get("Connection")

            # ResourceType 15 = CD Drive, 16 = DVD Drive
            if res_type in ["15", "16"] or "cd" in element_name.lower() or "dvd" in element_name.lower():
                connected = False
                media_name = None

                if eject_link:
                    media_name = eject_link.get("name") or host_resource
                    connected = True
                elif conn_val is not None:
                    connected = str(conn_val).lower() in ["true", "1"]

                device_type = map_device_type(sub_type, host_resource, media_name)

                devices.append({
                    "name": element_name if element_name else "CD/DVD drive 1",
                    "device_type": device_type,
                    "connected": connected
                })
        if devices:
            return devices
    except Exception:
        pass

    # 2. Try parsing XML
    try:
        root = ET.fromstring(text)
        eject_link = next((elem for elem in root.iter() if "media:ejectMedia" in elem.attrib.get("rel", "")), None)

        for item in root.iter():
            tag = item.tag.split("}")[-1]
            if tag == "Item":
                res_type = ""
                element_name = ""
                sub_type = ""
                host_resource = ""
                connected_found = None

                for child in item:
                    ctag = child.tag.split("}")[-1]
                    if ctag == "ResourceType":
                        res_type = (child.text or "").strip()
                    elif ctag == "ElementName":
                        element_name = (child.text or "").strip()
                    elif ctag == "ResourceSubType":
                        sub_type = (child.text or "").strip()
                    elif ctag == "HostResource":
                        host_resource = (child.text or "").strip()
                    elif ctag in ["Connected", "Connection"]:
                        connected_found = (child.text or "").strip().lower() in ["true", "1"]

                if res_type in ["15", "16"] or "cd" in element_name.lower() or "dvd" in element_name.lower():
                    connected = False
                    media_name = None

                    if eject_link is not None:
                        media_name = eject_link.attrib.get("name") or host_resource
                        connected = True
                    elif connected_found is not None:
                        connected = connected_found

                    device_type = map_device_type(sub_type, host_resource, media_name)

                    devices.append({
                        "name": element_name if element_name else "CD/DVD drive 1",
                        "device_type": device_type,
                        "connected": connected
                    })
    except Exception:
        pass

    return devices


def query_vm_cd_dvd(vcd_url, headers, ctx, name, vm_id, verbose=False):
    """
    Dynamically queries the media hardware section of the VM via vCD API.
    """
    base_vcd_url = vcd_url.rstrip("/")
    if base_vcd_url.endswith("/api"):
        base_vcd_url = base_vcd_url[:-4]

    uuid = vm_id.split(":")[-1]

    candidate_urls = [
        f"{base_vcd_url}/api/vApp/vm-{uuid}/virtualHardwareSection/media",
        f"{base_vcd_url}/api/vApp/vm-{uuid}"
    ]

    for url in candidate_urls:
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
                body = resp.read()
                devices = parse_cd_dvd_from_body(body)
                if devices:
                    if verbose:
                        sys.stderr.write(f"[INFO] Found {len(devices)} CD/DVD device(s) for {name} on {url}\n")
                    return devices
        except urllib.error.HTTPError as he:
            if verbose:
                sys.stderr.write(f"[DEBUG] HTTP {he.code} for {name} on {url}\n")
        except Exception as e:
            if verbose:
                sys.stderr.write(f"[DEBUG] Failed for {name} on {url}: {e}\n")

    return []


def get_cd_dvd():
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
        sys.stderr.write("[WARNING] Could not authenticate to vCD; returning empty list.\n")
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
        devices = query_vm_cd_dvd(vcd_url, headers, ctx, name, vm_id, verbose=verbose)
        results[name] = json.dumps(devices)

    print(json.dumps(results))


if __name__ == "__main__":
    get_cd_dvd()
