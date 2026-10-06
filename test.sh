#!/usr/bin/env bash
# ==============================================================================
# test.sh - Standalone test runner for get_snapshots.py
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
echo "  VMware Cloud Director Snapshot Diagnostic Test"
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

# Import get_snapshots functions
from get_snapshots import (
    build_ssl_context,
    get_auth_token,
    parse_snapshots_from_body,
    query_vm_snapshots
)

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

print("[STEP 1] Testing Authentication to VMware Cloud Director...")
token = get_auth_token(vcd_url, api_token, org, user, password, ctx, verbose=True)

if not token:
    print("\n[FAIL] Authentication failed! Unable to obtain access token.")
    print("Please verify your vcd_url, vcd_api_token, or credentials.")
    sys.exit(1)

print("\n[SUCCESS] Authentication token obtained.")
print(f"Token (preview): {token[:15]}...{token[-10:] if len(token) > 25 else ''}\n")

headers = {
    "Accept": "application/*+json;version=37.0, application/*+xml;version=37.0, application/json, */*",
    "Authorization": f"Bearer {token}",
    "X-VMWARE-VCLOUD-ACCESS-TOKEN": token,
    "x-vcloud-authorization": token
}

base_vcd_url = vcd_url[:-4] if vcd_url.endswith("/api") else vcd_url
urn = vm_id if vm_id.startswith("urn:vcloud:vm:") else f"urn:vcloud:vm:{vm_id}"
uuid = vm_id.split(":")[-1]

print(f"[STEP 2] Probing Snapshot Endpoints for VM '{vm_name}' (UUID: {uuid})...")

endpoints = [
    ("OpenAPI (singular URN)", f"{base_vcd_url}/cloudapi/1.0.0/vm/{urn}/snapshots"),
    ("OpenAPI (plural URN)",   f"{base_vcd_url}/cloudapi/1.0.0/vms/{urn}/snapshots"),
    ("OpenAPI (singular UUID)",f"{base_vcd_url}/cloudapi/1.0.0/vm/{uuid}/snapshots"),
    ("OpenAPI (plural UUID)",  f"{base_vcd_url}/cloudapi/1.0.0/vms/{uuid}/snapshots"),
    ("Legacy SnapshotSection", f"{base_vcd_url}/api/vApp/vm-{uuid}/snapshotSection"),
    ("Legacy VM Representation",f"{base_vcd_url}/api/vApp/vm-{uuid}"),
]

found_any = False
for label, url in endpoints:
    print(f"\n--- Testing: {label} ---")
    print(f"URL: {url}")
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            status = resp.status
            body = resp.read()
            body_preview = body.decode("utf-8", errors="replace").strip()
            print(f"HTTP Status: {status}")
            print(f"Response Preview (first 300 chars):")
            print(body_preview[:300])
            
            snaps = parse_snapshots_from_body(body)
            if snaps:
                print(f"--> [SUCCESS] Parsed {len(snaps)} snapshot(s): {snaps}")
                found_any = True
            else:
                print("--> [INFO] Endpoint responded, but 0 snapshots were parsed.")
    except urllib.error.HTTPError as he:
        print(f"HTTP Error {he.code}: {he.reason}")
        err_body = he.read().decode("utf-8", errors="replace").strip()
        if err_body:
            print(f"Error Response (first 200 chars): {err_body[:200]}")
    except Exception as ex:
        print(f"Connection Failed: {ex}")

print("\n" + "="*60)
print("[STEP 3] Running full get_snapshots.py via Terraform input protocol...")
import subprocess

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

print(f"Process Exit Code: {proc.returncode}")
if stderr:
    print("\nSTDERR Output:")
    print(stderr)

print("\nSTDOUT (Terraform Data Source Output):")
print(stdout)

print("="*60)
if found_any:
    print("[RESULT] Snapshot diagnostic completed successfully! Snapshots detected.")
else:
    print("[RESULT] Diagnostic completed. If 0 snapshots were found, review endpoint responses above.")
print("="*60)
EOF
