#!/usr/bin/env bash
# ==============================================================================
# test.sh - Standalone test runner for get_snapshots.py and get_cd_dvd.py
#
# Usage:
#   bash test.sh
#   bash test.sh <VM_NAME> <VM_ID>
#   bash test.sh eks-bake urn:vcloud:vm:a6441e5-2fb0-4f26-b97f-c64c45039266
# ==============================================================================

set -e

# Detect Python executable
if command -v python3 &>/dev/null; then
    PYTHON_CMD="python3"
elif command -v python &>/dev/null; then
    PYTHON_CMD="python"
else
    echo "[ERROR] Python is not installed or not in PATH."
    exit 1
fi

echo "============================================================"
echo "  VMware Cloud Director Hardware & Snapshot Diagnostic Test"
echo "============================================================"

# Auto-parse terraform.auto.tfvars if present and env vars are not set
TFVARS="terraform.auto.tfvars"
if [ -f "$TFVARS" ]; then
    echo "[INFO] Reading configuration from $TFVARS..."
    [ -z "$VCD_URL" ] && VCD_URL=$(grep -E '^\s*vcd_url\s*=' "$TFVARS" | head -n1 | sed -E 's/^\s*vcd_url\s*=\s*"(.*)"/\1/')
    [ -z "$VCD_ORG" ] && VCD_ORG=$(grep -E '^\s*vcd_org\s*=' "$TFVARS" | head -n1 | sed -E 's/^\s*vcd_org\s*=\s*"(.*)"/\1/')
    [ -z "$VCD_USER" ] && VCD_USER=$(grep -E '^\s*vcd_user\s*=' "$TFVARS" | head -n1 | sed -E 's/^\s*vcd_user\s*=\s*"(.*)"/\1/')
    [ -z "$VCD_PASSWORD" ] && VCD_PASSWORD=$(grep -E '^\s*vcd_password\s*=' "$TFVARS" | head -n1 | sed -E 's/^\s*vcd_password\s*=\s*"(.*)"/\1/')
    [ -z "$VCD_API_TOKEN" ] && VCD_API_TOKEN=$(grep -E '^\s*vcd_api_token\s*=' "$TFVARS" | head -n1 | sed -E 's/^\s*vcd_api_token\s*=\s*"(.*)"/\1/')
fi

# Fallback defaults or prompt
VCD_URL="${VCD_URL:-}"
VCD_ORG="${VCD_ORG:-DEMO2}"
VCD_API_TOKEN="${VCD_API_TOKEN:-}"
VCD_USER="${VCD_USER:-}"
VCD_PASSWORD="${VCD_PASSWORD:-}"

# VM to test
TARGET_VM_NAME="${1:-}"
TARGET_VM_ID="${2:-}"

# If no VM passed, try to detect from terraform.tfstate
if [ -z "$TARGET_VM_NAME" ] && [ -f "terraform.tfstate" ]; then
    echo "[INFO] Detecting VM names from terraform.tfstate..."
    DETECTED_VMS=$($PYTHON_CMD -c '
import json, sys
try:
    with open("terraform.tfstate") as f:
        data = json.load(f)
    for res in data.get("resources", []):
        if res.get("type") == "vcd_vm" and res.get("mode") == "data":
            for inst in res.get("instances", []):
                attrs = inst.get("attributes", {})
                name = attrs.get("name")
                vmid = attrs.get("id")
                if name and vmid:
                    print(f"{name}|{vmid}")
except Exception:
    pass
')
    if [ -n "$DETECTED_VMS" ]; then
        FIRST_VM=$(echo "$DETECTED_VMS" | head -n1)
        TARGET_VM_NAME=$(echo "$FIRST_VM" | cut -d'|' -f1)
        TARGET_VM_ID=$(echo "$FIRST_VM" | cut -d'|' -f2)
    fi
fi

# Fallback to eks-bake if still empty
if [ -z "$TARGET_VM_NAME" ]; then
    TARGET_VM_NAME="eks-bake"
    TARGET_VM_ID="urn:vcloud:vm:eks-bake"
fi

echo "Configuration:"
echo "  VCD URL       : ${VCD_URL:-<NOT SET>}"
echo "  VCD Org       : ${VCD_ORG:-<NOT SET>}"
echo "  Target VM     : $TARGET_VM_NAME"
echo "  Target VM ID  : $TARGET_VM_ID"
echo "  Token present : $([ -n "$VCD_API_TOKEN" ] && echo "YES" || echo "NO")"
echo "============================================================"
echo ""

$PYTHON_CMD - <<EOF
import sys
import json
import urllib.request
import urllib.parse
import ssl
import base64
import os
import subprocess

# Import functions
from get_snapshots import (
    build_ssl_context,
    get_auth_token,
    parse_snapshots_from_body
)
from get_cd_dvd import parse_cd_dvd_from_body

vcd_url = "$VCD_URL".rstrip("/")
api_token = "$VCD_API_TOKEN"
org = "$VCD_ORG"
user = "$VCD_USER"
password = "$VCD_PASSWORD"
vm_name = "$TARGET_VM_NAME"
vm_id = "$TARGET_VM_ID"

if not vcd_url:
    print("[ERROR] VCD_URL is empty! Please set VCD_URL or create terraform.auto.tfvars")
    sys.exit(1)

ctx = build_ssl_context()

print("[STEP 1] Authenticating to VMware Cloud Director...")
token = get_auth_token(vcd_url, api_token, org, user, password, ctx, verbose=True)

if not token:
    print("\n[FAIL] Authentication failed! Unable to obtain access token.")
    print("Please verify your vcd_url, vcd_api_token, or credentials.")
    sys.exit(1)

print(f"\n[SUCCESS] Authentication token obtained.")
print(f"Token preview: {token[:15]}...{token[-8:] if len(token) > 23 else ''}\n")

headers = {
    "Accept": "application/*+json;version=37.0, application/*+xml;version=37.0, application/json, */*",
    "Authorization": f"Bearer {token}",
    "X-VMWARE-VCLOUD-ACCESS-TOKEN": token,
    "x-vcloud-authorization": token
}

base_vcd_url = vcd_url[:-4] if vcd_url.endswith("/api") else vcd_url
urn = vm_id if vm_id.startswith("urn:vcloud:vm:") else f"urn:vcloud:vm:{vm_id}"
uuid = vm_id.split(":")[-1]

print("="*60)
print(f"[STEP 2] DYNAMICALLY PROBING SNAPSHOTS FOR '{vm_name}'...")
snapshot_endpoints = [
    ("OpenAPI (singular URN)", f"{base_vcd_url}/cloudapi/1.0.0/vm/{urn}/snapshots"),
    ("OpenAPI (plural URN)",   f"{base_vcd_url}/cloudapi/1.0.0/vms/{urn}/snapshots"),
    ("OpenAPI (singular UUID)",f"{base_vcd_url}/cloudapi/1.0.0/vm/{uuid}/snapshots"),
    ("OpenAPI (plural UUID)",  f"{base_vcd_url}/cloudapi/1.0.0/vms/{uuid}/snapshots"),
    ("Legacy SnapshotSection", f"{base_vcd_url}/api/vApp/vm-{uuid}/snapshotSection"),
    ("Legacy VM Representation",f"{base_vcd_url}/api/vApp/vm-{uuid}"),
]

found_snaps = []
for label, url in snapshot_endpoints:
    print(f"\nEndpoint: {label}")
    print(f"URL: {url}")
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            body = resp.read()
            body_preview = body.decode("utf-8", errors="replace").strip()
            print(f"HTTP Status: {resp.status}")
            print(f"Raw Response (first 250 chars): {body_preview[:250]}")
            snaps = parse_snapshots_from_body(body)
            if snaps:
                print(f"--> [MATCH] Dynamically parsed {len(snaps)} snapshot(s): {snaps}")
                found_snaps = snaps
                break
            else:
                print("--> Responded, but no snapshots contained in payload.")
    except urllib.error.HTTPError as he:
        print(f"HTTP Error {he.code}: {he.reason}")
    except Exception as ex:
        print(f"Connection error: {ex}")

print("\n" + "="*60)
print(f"[STEP 3] DYNAMICALLY PROBING CD/DVD HARDWARE FOR '{vm_name}'...")
cd_endpoints = [
    ("Virtual Hardware Media", f"{base_vcd_url}/api/vApp/vm-{uuid}/virtualHardwareSection/media"),
    ("VM Representation",      f"{base_vcd_url}/api/vApp/vm-{uuid}"),
]

found_cds = []
for label, url in cd_endpoints:
    print(f"\nEndpoint: {label}")
    print(f"URL: {url}")
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            body = resp.read()
            body_preview = body.decode("utf-8", errors="replace").strip()
            print(f"HTTP Status: {resp.status}")
            print(f"Raw Hardware Response (first 350 chars):")
            print(body_preview[:350])
            devices = parse_cd_dvd_from_body(body)
            if devices:
                print(f"--> [MATCH] Dynamically parsed {len(devices)} CD/DVD device(s): {devices}")
                found_cds = devices
                break
            else:
                print("--> No CD/DVD devices found in this section.")
    except urllib.error.HTTPError as he:
        print(f"HTTP Error {he.code}: {he.reason}")
    except Exception as ex:
        print(f"Connection error: {ex}")

print("\n" + "="*60)
print("[STEP 4] RUNNING get_snapshots.py (Terraform Protocol Output)...")
query_payload = json.dumps({
    "vcd_url": vcd_url,
    "vcd_api_token": api_token,
    "vcd_org": org,
    "vcd_user": user,
    "vcd_password": password,
    "verbose": True,
    "vm_ids": json.dumps({vm_name: vm_id})
})

proc = subprocess.Popen(
    ["$PYTHON_CMD", "get_snapshots.py"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)
stdout, stderr = proc.communicate(input=query_payload)
print(f"get_snapshots.py output: {stdout.strip()}")

print("\n" + "="*60)
print("[STEP 5] RUNNING get_cd_dvd.py (Terraform Protocol Output)...")
proc2 = subprocess.Popen(
    ["$PYTHON_CMD", "get_cd_dvd.py"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True
)
stdout2, stderr2 = proc2.communicate(input=query_payload)
print(f"get_cd_dvd.py output: {stdout2.strip()}")

print("\n" + "="*60)
print("DIAGNOSTIC SUMMARY:")
print(f"  Snapshots found: {found_snaps}")
print(f"  CD/DVD found   : {found_cds}")
print("="*60)
EOF
