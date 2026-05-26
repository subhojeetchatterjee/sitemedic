import hmac, hashlib, json, os, urllib.request

secret = os.environ['DT_WEBHOOK_SECRET']
payload_dict = {
    'ProblemID': 'P-12350',
    'ProblemTitle': 'High error rate on demo app',
    'Severity': 'ERROR',
    'State': 'OPEN',
    'ImpactedEntities': [{'name': 'sitemedic-demo-app', 'entityId': 'SERVICE-DEMO'}]
}
body_bytes = json.dumps(payload_dict, separators=(',', ':')).encode('utf-8')
sig = 'sha256=' + hmac.new(secret.encode('utf-8'), body_bytes, hashlib.sha256).hexdigest()
req = urllib.request.Request(
    'http://localhost:8080/api/webhooks/dynatrace',
    data=body_bytes,
    headers={'Content-Type': 'application/json', 'X-Hub-Signature-256': sig},
    method='POST'
)
with urllib.request.urlopen(req) as r:
    print(r.read().decode())
