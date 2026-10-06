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
    Authenticates with VMware Cloud Director via:
    1. OAuth refresh token exchange at /oauth/provider/token or /oauth/tenant/{org}/token
    2. Basic auth session creation at /api/sessions
    3. Direct api_token fallback
    """
    if api_token:
        oauth_endpoints = [f"{vcd_url}/oauth/provider/token"]
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

    return api_token or None


def parse_cd_dvd_from_body(raw_body):
    """
    Parses CD/DVD drive devices from vCD XML or JSON response.
    Extracts name (e.g. 'CD/DVD drive 1'), device_type ('Host Device' or ISO name),
    and connected status (True/False).
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
            host_resource = str(item.get("hostResource") or item.get("HostResource") or "")
            conn_val = item.get("connected") or item.get("Connected") or item.get("connection") or item.get("Connection")
            connected = str(conn_val).lower() in ["true", "1"]

            if res_type in ["15", "16"] or "cd/dvd" in element_name.lower():
                device_type = "Host Device"
                if eject_link:
                    media_name = eject_link.get("name") or host_resource or "Datastore ISO file"
                    device_type = media_name
                    connected = True
                elif host_resource and host_resource.strip():
                    device_type = host_resource.strip()

                devices.append({
                    "name": element_name or "CD/DVD drive 1",
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
            if item.tag.endswith("Item"):
                res_type = ""
                element_name = ""
                host_resource = ""
                connected = False
                for child in item:
                    tag = child.tag.split("}")[-1]
                    if tag == "ResourceType":
                        res_type = (child.text or "").strip()
                    elif tag == "ElementName":
                        element_name = (child.text or "").strip()
                    elif tag == "HostResource":
                        host_resource = (child.text or "").strip()
                    elif tag in ["Connected", "Connection"]:
                        connected = (child.text or "").strip().lower() in ["true", "1"]

                # ResourceType 15 = CD Drive, 16 = DVD Drive
                if res_type in ["15", "16"] or "cd/dvd" in element_name.lower():
                    device_type = "Host Device"
                    if eject_link is not None:
                        media_name = eject_link.attrib.get("name") or host_resource or "Datastore ISO file"
                        device_type = media_name
                        connected = True
                    elif host_resource and host_resource.strip():
                        device_type = host_resource.strip()

                    devices.append({
                        "name": element_name or "CD/DVD drive 1",
                        "device_type": device_type,
                        "connected": connected
                    })
    except Exception:
        pass

    return devices


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

    try:
        vm_ids = json.loads(query.get("vm_ids", "{}"))
    except Exception:
        vm_ids = {}

    default_result = [{
        "name": "CD/DVD drive 1",
        "device_type": "Host Device",
        "connected": False
    }]

    if not vcd_url or not vm_ids:
        print(json.dumps({name: json.dumps(default_result) for name in vm_ids}))
        return

    ctx = build_ssl_context()
    token = get_auth_token(vcd_url, api_token, org, user, password, ctx)

    if not token:
        sys.stderr.write("[WARNING] Could not authenticate to vCD; returning default CD/DVD drive.\n")
        print(json.dumps({name: json.dumps(default_result) for name in vm_ids}))
        return

    headers = {
        "Accept": "application/*+xml;version=37.0, application/json, */*",
        "Authorization": f"Bearer {token}",
        "X-VMWARE-VCLOUD-ACCESS-TOKEN": token,
        "x-vcloud-authorization": token
    }

    results = {}
    for name, vm_id in vm_ids.items():
        uuid = vm_id.split(":")[-1]

        candidate_urls = [
            f"{vcd_url}/api/vApp/vm-{uuid}/virtualHardwareSection/media",
            f"{vcd_url}/api/vApp/vm-{uuid}"
        ]

        vm_cd_dvd = []
        for url in candidate_urls:
            try:
                req = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                    body = resp.read()
                    devices = parse_cd_dvd_from_body(body)
                    if devices:
                        vm_cd_dvd = devices
                        break
            except Exception as e:
                sys.stderr.write(f"[DEBUG] CD/DVD check failed for {name} on {url}: {e}\n")

        if not vm_cd_dvd:
            vm_cd_dvd = default_result

        results[name] = json.dumps(vm_cd_dvd)

    print(json.dumps(results))


if __name__ == "__main__":
    get_cd_dvd()
