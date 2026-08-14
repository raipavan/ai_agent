import urllib.request, json, base64, os

# Get QR from OpenWA
api_key = "owa_k1_75914890de773c2a9d72c5e54a8a749316573475c6012c6e009f72b7cbd9bb80"
session_id = "a302b29f-3b81-4bcd-a3cf-db205367ab6e"
out_dir = "/opt/data-edge/Data-Edge/frontend/static"

req = urllib.request.Request(
    f"http://127.0.0.1:2785/api/sessions/{session_id}/qr",
    headers={"Authorization": f"Bearer {api_key}"}
)
resp = urllib.request.urlopen(req)
data = json.loads(resp.read())

qr_data_uri = data.get("qrCode", "")
if not qr_data_uri:
    print("ERROR: No QR code returned from OpenWA")
    print("Response:", json.dumps(data, indent=2)[:500])
    exit(1)

print(f"QR code received ({len(qr_data_uri)} chars)")

# Extract base64 data
if qr_data_uri.startswith("data:image"):
    b64 = qr_data_uri.split(",")[1] if "," in qr_data_uri else qr_data_uri
    png_bytes = base64.b64decode(b64)
    png_path = os.path.join(out_dir, "whatsapp_qr.png")
    with open(png_path, "wb") as f:
        f.write(png_bytes)
    print(f"QR PNG saved to {png_path} ({len(png_bytes)} bytes)")
else:
    print(f"Unexpected QR format: {qr_data_uri[:100]}")
    exit(1)
