"""
Runs 5 incidents through the full lifecycle with demo recording active.
Each incident: webhook → DIAGNOSING → AWAITING_APPROVAL → approve → RESOLVED + postmortem
"""
import hmac, hashlib, json, os, time, urllib.request, urllib.error

SECRET = os.environ['DT_WEBHOOK_SECRET']
API_KEY = os.environ['AGENT_API_KEY']
BASE = 'http://localhost:8080'

INCIDENTS = [
    {'id': 'P-REC-001', 'title': 'High error rate on demo app',       'severity': 'ERROR'},
    {'id': 'P-REC-002', 'title': 'High memory utilization on demo app','severity': 'ERROR'},
    {'id': 'P-REC-003', 'title': 'Slow response time on demo app',    'severity': 'PERFORMANCE'},
    {'id': 'P-REC-004', 'title': 'Service availability degraded',     'severity': 'AVAILABILITY'},
    {'id': 'P-REC-005', 'title': 'CPU spike on demo app',             'severity': 'ERROR'},
]


def send_webhook(incident: dict) -> dict:
    payload = {
        'ProblemID': incident['id'],
        'ProblemTitle': incident['title'],
        'Severity': incident['severity'],
        'State': 'OPEN',
        'ImpactedEntities': [{'name': 'sitemedic-demo-app', 'entityId': 'SERVICE-DEMO'}],
    }
    body = json.dumps(payload, separators=(',', ':')).encode()
    sig = 'sha256=' + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f'{BASE}/api/webhooks/dynatrace',
        data=body,
        headers={'Content-Type': 'application/json', 'X-Hub-Signature-256': sig},
        method='POST',
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def get_incident(pid: str) -> dict:
    req = urllib.request.Request(f'{BASE}/api/incidents/{pid}')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def approve_incident(pid: str) -> dict:
    body = json.dumps({'approved': True, 'operator': 'demo-recorder'}).encode()
    req = urllib.request.Request(
        f'{BASE}/api/incidents/{pid}/approve',
        data=body,
        headers={'Content-Type': 'application/json', 'X-API-Key': API_KEY},
        method='POST',
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def poll_until(pid: str, target_statuses: list, timeout: int = 300) -> str:
    deadline = time.time() + timeout
    last_status = ''
    last_steps = 0
    while time.time() < deadline:
        inc = get_incident(pid)
        status = inc['status']
        steps = len(inc.get('trace', []))
        if status != last_status or steps != last_steps:
            print(f'    [{pid}] {status} ({steps} trace steps)')
            last_status = status
            last_steps = steps
        if status in target_statuses:
            return status
        time.sleep(8)
    raise TimeoutError(f'{pid} did not reach {target_statuses} within {timeout}s')


def run_incident(incident: dict, index: int, total: int):
    pid = incident['id']
    print(f'\n[{index}/{total}] Starting incident {pid}: {incident["title"]}')

    print(f'  Sending webhook...')
    result = send_webhook(incident)
    print(f'  Created: {result}')

    print(f'  Waiting for AWAITING_APPROVAL...')
    poll_until(pid, ['AWAITING_APPROVAL'], timeout=300)

    print(f'  Approving...')
    approve_result = approve_incident(pid)
    print(f'  Approval: {approve_result}')

    print(f'  Waiting for RESOLVED...')
    poll_until(pid, ['RESOLVED'], timeout=300)

    inc = get_incident(pid)
    plan_action = (inc.get('plan') or {}).get('action', 'none')
    has_postmortem = bool(inc.get('postmortem'))
    print(f'  Done! plan={plan_action} postmortem={has_postmortem}')
    return inc


if __name__ == '__main__':
    print(f'Running {len(INCIDENTS)} incidents with demo recording active\n')
    results = []
    for i, incident in enumerate(INCIDENTS, 1):
        try:
            inc = run_incident(incident, i, len(INCIDENTS))
            results.append({'id': incident['id'], 'status': 'ok', 'plan': (inc.get('plan') or {}).get('action')})
        except Exception as e:
            print(f'  ERROR: {e}')
            results.append({'id': incident['id'], 'status': 'error', 'error': str(e)})

    print('\n=== Summary ===')
    for r in results:
        print(f"  {r['id']}: {r['status']}" + (f" plan={r.get('plan')}" if r['status'] == 'ok' else f" err={r.get('error')}"))
