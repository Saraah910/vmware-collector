#!/usr/bin/env python3
"""
vcd_get_media.py
----------------
Fetches Removable Media (CD/DVD drive and Floppy drive) details for a VM in VMware Cloud Director.

Supports authentication via:
1. API Token (Bearer token from VCD User Preferences)
2. Username + Password + Org Name
"""

import argparse
import json
import sys
import xml.etree.ElementTree as ET
import urllib3
import requests

# Suppress insecure HTTPS warnings if --insecure is passed
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def parse_vm_id(vm_id: str) -> str:
    """
    Normalizes VM ID to the format required by the vCloud REST API: vm-<uuid>
    Handles formats like:
      - urn:vcloud:vm:c8e1e779-1111-2222-3333-444455556666
      - vm-c8e1e779-1111-2222-3333-444455556666
      - c8e1e779-1111-2222-3333-444455556666
    """
    vm_id = vm_id.strip()
    if vm_id.startswith("urn:vcloud:vm:"):
        return vm_id.replace("urn:vcloud:vm:", "vm-")
    if not vm_id.startswith("vm-") and not vm_id.startswith("http"):
        return f"vm-{vm_id}"
    return vm_id


def get_vcd_session(vcd_url: str, org_name: str, user: str = None, password: str = None,
                    api_token: str = None, api_version: str = "39.1", verify_ssl: bool = True):
    """
    Authenticates against VMware Cloud Director and returns session headers.
    """
    session_endpoint = f"{vcd_url.rstrip('/')}/api/sessions"
    headers = {
        "Accept": f"application/*+json;version={api_version}"
    }

    if api_token:
        print("[*] Authenticating using API Token...")
        headers["Authorization"] = f"Bearer {api_token}"
        response = requests.post(session_endpoint, headers=headers, verify=verify_ssl)
    elif user and password:
        user_with_org = f"{user}@{org_name}"
        print(f"[*] Authenticating using credentials for '{user_with_org}'...")
        response = requests.post(session_endpoint, auth=(user_with_org, password),
                                 headers=headers, verify=verify_ssl)
    else:
        raise ValueError("Either 'api_token' or both 'user' and 'password' must be supplied.")

    if not response.ok:
        raise RuntimeError(f"Authentication failed ({response.status_code}): {response.text}")

    # Extract tokens from response headers
    vcloud_token = response.headers.get("X-VMWARE-VCLOUD-ACCESS-TOKEN") or response.headers.get("x-vcloud-authorization")
    if not vcloud_token:
        vcloud_token = response.headers.get("x-vmware-vcloud-access-token")

    if not vcloud_token:
        raise RuntimeError("Authentication succeeded but no authorization token found in response headers.")

    print("[+] Successfully authenticated with VMware Cloud Director.")
    
    auth_headers = {
        "Accept": f"application/*+json;version={api_version}",
        "Authorization": f"Bearer {vcloud_token}",
        "x-vcloud-authorization": vcloud_token
    }
    return auth_headers


def get_vm_removable_media(vcd_url: str, vm_id: str, auth_headers: dict,
                           api_version: str = "39.1", verify_ssl: bool = True):
    """
    Queries the VM's virtualHardwareSection/media endpoint.
    """
    clean_vm_id = parse_vm_id(vm_id)

    if clean_vm_id.startswith("http"):
        endpoint = f"{clean_vm_id.rstrip('/')}/virtualHardwareSection/media"
    else:
        endpoint = f"{vcd_url.rstrip('/')}/api/vApp/{clean_vm_id}/virtualHardwareSection/media"

    print(f"[*] Querying endpoint: {endpoint}")
    response = requests.get(endpoint, headers=auth_headers, verify=verify_ssl)

    # Fallback to full virtualHardwareSection if /media sub-path returns 404
    if response.status_code == 404:
        fallback_endpoint = endpoint.replace("/virtualHardwareSection/media", "/virtualHardwareSection")
        print(f"[!] /media returned 404, checking full hardware section: {fallback_endpoint}")
        response = requests.get(fallback_endpoint, headers=auth_headers, verify=verify_ssl)

    if not response.ok:
        raise RuntimeError(f"Failed to fetch media details ({response.status_code}): {response.text}")

    content_type = response.headers.get("Content-Type", "")
    return response, content_type


def parse_response_content(response, content_type):
    """
    Parses JSON and XML responses and extracts CD/DVD and Floppy details.
    """
    parsed_devices = []

    # 1. Attempt JSON parsing
    if "json" in content_type:
        try:
            data = response.json()
            items = []
            if "item" in data:
                items = data["item"] if isinstance(data["item"], list) else [data["item"]]
            elif "Item" in data:
                items = data["Item"] if isinstance(data["Item"], list) else [data["Item"]]
            
            for item in items:
                rtype = str(item.get("resourceType") or item.get("rasd:ResourceType") or "")
                # 15 = CD/DVD Drive, 14 = Floppy Drive, 16 = DVD Drive
                if rtype in ["14", "15", "16"]:
                    type_label = "CD/DVD drive" if rtype in ["15", "16"] else "Floppy drive"
                    host_res = item.get("hostResource") or item.get("rasd:HostResource")
                    status = "Connected" if host_res else "Disconnected"
                    
                    media_name, media_href = None, None
                    if isinstance(host_res, dict):
                        media_name = host_res.get("name")
                        media_href = host_res.get("href")
                    elif isinstance(host_res, list) and len(host_res) > 0:
                        first = host_res[0]
                        if isinstance(first, dict):
                            media_name = first.get("name")
                            media_href = first.get("href")

                    actions = []
                    links = item.get("link") or item.get("Link") or []
                    if isinstance(links, dict):
                        links = [links]
                    for link in links:
                        rel = link.get("rel")
                        if rel in ["insertMedia", "ejectMedia"]:
                            actions.append(f"{rel} ({link.get('href')})")

                    parsed_devices.append({
                        "name": item.get("elementName") or item.get("rasd:ElementName"),
                        "type_label": type_label,
                        "resource_type": rtype,
                        "sub_type": item.get("resourceSubType") or item.get("rasd:ResourceSubType"),
                        "status": status,
                        "media_name": media_name,
                        "media_href": media_href,
                        "actions": actions
                    })
            return data, parsed_devices
        except Exception:
            pass

    # 2. XML fallback parsing
    try:
        root = ET.fromstring(response.text)
        namespaces = {
            'vcloud': 'http://www.vmware.com/vcloud/v1.5',
            'rasd': 'http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData'
        }
        
        items = root.findall('.//{http://schemas.dmtf.org/ovf/envelope/1}Item') or root.findall('.//Item')

        for item in items:
            rtype_elem = item.find('rasd:ResourceType', namespaces) or \
                         item.find('{http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData}ResourceType')
            rtype = rtype_elem.text.strip() if rtype_elem is not None and rtype_elem.text else ""
            
            if rtype in ["14", "15", "16"]:
                type_label = "CD/DVD drive" if rtype in ["15", "16"] else "Floppy drive"
                name_elem = item.find('rasd:ElementName', namespaces) or \
                            item.find('{http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData}ElementName')
                name = name_elem.text if name_elem is not None else "Unknown"

                subtype_elem = item.find('rasd:ResourceSubType', namespaces) or \
                               item.find('{http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData}ResourceSubType')
                subtype = subtype_elem.text if subtype_elem is not None else ""

                host_res_elem = item.find('rasd:HostResource', namespaces) or \
                                item.find('{http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData}HostResource')
                status = "Connected" if host_res_elem is not None and (host_res_elem.text or host_res_elem.attrib) else "Disconnected"
                media_name = host_res_elem.attrib.get('name') if host_res_elem is not None else None
                media_href = host_res_elem.attrib.get('href') if host_res_elem is not None else None

                parsed_devices.append({
                    "name": name,
                    "type_label": type_label,
                    "resource_type": rtype,
                    "sub_type": subtype,
                    "status": status,
                    "media_name": media_name,
                    "media_href": media_href,
                    "actions": []
                })

        return response.text, parsed_devices
    except Exception:
        return response.text, []


def format_parsed_devices(devices):
    print("\n" + "=" * 70)
    print("                REMOVABLE MEDIA DETAILS")
    print("=" * 70)
    if not devices:
        print("No removable media devices (CD/DVD or Floppy) found.")
        return

    for idx, dev in enumerate(devices, start=1):
        print(f"Device #{idx}:")
        print(f"  Name           : {dev.get('name', 'N/A')}")
        print(f"  Type           : {dev.get('type_label', 'Unknown')}")
        print(f"  Resource Type  : {dev.get('resource_type', 'N/A')}")
        print(f"  Sub Type       : {dev.get('sub_type', 'N/A')}")
        print(f"  Status         : {dev.get('status', 'Unknown')}")
        if dev.get('media_name'):
            print(f"  Mounted Media  : {dev.get('media_name')}")
        if dev.get('media_href'):
            print(f"  Media Link     : {dev.get('media_href')}")
        if dev.get('actions'):
            print(f"  Actions        : {', '.join(dev['actions'])}")
        print("-" * 70)


def main():
    parser = argparse.ArgumentParser(description="Fetch Removable Media (CD/DVD/Floppy) details for a VM in VMware Cloud Director.")
    parser.add_argument("--vcd-url", required=True, help="Base URL of VCD (e.g., https://vcd.example.com)")
    parser.add_argument("--vcd-org-name", default="System", help="Organization name (default: System)")
    parser.add_argument("--user", default=None, help="VCD username")
    parser.add_argument("--password", default=None, help="VCD password")
    parser.add_argument("--api-token", default=None, help="VCD API Token / Refresh Token (preferred)")
    parser.add_argument("--api-version", default="39.1", help="API Version (default: 39.1)")
    parser.add_argument("--vm-id", required=True, help="Virtual Machine ID (vm-uuid, urn:vcloud:vm:uuid, or uuid)")
    parser.add_argument("--insecure", "-k", action="store_true", help="Disable SSL certificate verification")

    args = parser.parse_args()
    verify_ssl = not args.insecure

    # 1. Authenticate
    auth_headers = get_vcd_session(
        vcd_url=args.vcd_url,
        org_name=args.vcd_org_name,
        user=args.user,
        password=args.password,
        api_token=args.api_token,
        api_version=args.api_version,
        verify_ssl=verify_ssl
    )

    # 2. Query Removable Media
    response, content_type = get_vm_removable_media(
        vcd_url=args.vcd_url,
        vm_id=args.vm_id,
        auth_headers=auth_headers,
        api_version=args.api_version,
        verify_ssl=verify_ssl
    )

    # 3. Print parsed output
    raw_content, devices = parse_response_content(response, content_type)
    format_parsed_devices(devices)

    # 4. Print raw response
    print("\n[+] RAW API RESPONSE:")
    if isinstance(raw_content, dict):
        print(json.dumps(raw_content, indent=2))
    else:
        print(raw_content)


if __name__ == "__main__":
    main()