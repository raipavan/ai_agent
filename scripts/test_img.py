import urllib.request, json

api_key = "owa_k1_75914890de773c2a9d72c5e54a8a749316573475c6012c6e009f72b7cbd9bb80"
session_id = "ab438757-493f-4160-9f13-a39622a24483"

# Test send-image with base64 (tiny 1x1 red pixel PNG)
import base64
png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAoAAAAKCAYAAACNMs+9AAAAFklEQVQYV2P8z8BQz0BFwMgwasKoOgBnLwIHXwJ2xAAAAABJRU5ErkJggg=="

body = json.dumps({
    "chatId": "919999999999@c.us",
    "base64": png_b64,
    "mimetype": "image/png",
    "caption": "Test - Regal Edition"
}).encode()

req = urllib.request.Request(
    "http://127.0.0.1:2785/api/sessions/" + session_id + "/messages/send-image",
    data=body, method="POST",
    headers={"X-API-Key": api_key, "Content-Type": "application/json"}
)
try:
    resp = urllib.request.urlopen(req)
    print("OK:", resp.read().decode()[:200])
except Exception as e:
    body_text = ""
    if hasattr(e, "read"):
        body_text = e.read().decode()[:300]
    print(f"Error: {e} | {body_text}")

# Test send-document endpoint too
body2 = json.dumps({
    "chatId": "919999999999@c.us",
    "base64": png_b64,
    "mimetype": "application/pdf",
    "filename": "Maruti_Brochure.pdf"
}).encode()

req2 = urllib.request.Request(
    "http://127.0.0.1:2785/api/sessions/" + session_id + "/messages/send-document",
    data=body2, method="POST",
    headers={"X-API-Key": api_key, "Content-Type": "application/json"}
)
try:
    resp2 = urllib.request.urlopen(req2)
    print("Document OK:", resp2.read().decode()[:200])
except Exception as e:
    body_text = ""
    if hasattr(e, "read"):
        body_text = e.read().decode()[:300]
    print(f"Document Error: {e} | {body_text}")
