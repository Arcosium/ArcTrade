"""Switch only ArcTrade/QuantInSight/AutoCrypto routing. Default is read-only plan."""
import argparse
import copy
import json
import time
import urllib.request
from pathlib import Path

ACCOUNT='2caf6d4a635bbf686c9fa9522279a28b'
ZONE='2cd0c9cdd4d442112156d0eb4641a114'
TUNNEL='d076ab40-34ee-43fa-b070-ae357076d155'
ROUTES={'arctrade.ai-ve.uk':'http://127.0.0.1:8997',
        'quantinsight.ai-ve.uk':'http://127.0.0.1:8997',
        'autocrypto.ai-ve.uk':'http_status:410'}


def routed(config):
    result=copy.deepcopy(config)
    rules=[r for r in result['ingress'] if r.get('hostname') not in ROUTES]
    if not rules or rules[-1].get('hostname'):raise ValueError('Missing tunnel fallback')
    result['ingress']=rules[:-1]+[dict(hostname=h,service=s) for h,s in ROUTES.items()]+rules[-1:]
    return result


def main(apply=False):
    vault=Path.home()/'vault'
    token=next(x.strip() for x in (vault/'secrets_api_keys.txt').read_text().splitlines() if x.strip().startswith('cfut_'))
    def cf(path,method='GET',body=None):
        req=urllib.request.Request('https://api.cloudflare.com/client/v4'+path,method=method,
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},
            data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req,timeout=30) as r:result=json.load(r)
        if not result.get('success'):raise RuntimeError('Cloudflare request failed')
        return result['result']
    path=f'/accounts/{ACCOUNT}/cfd_tunnel/{TUNNEL}/configurations'
    before=cf(path);dns={h:cf(f'/zones/{ZONE}/dns_records?name={h}') for h in ROUTES}
    if any(len(records)!=1 or records[0]['type']!='CNAME' for records in dns.values()):
        raise RuntimeError('Expected one CNAME per existing host')
    print(json.dumps(dict(routes=ROUTES,apply=apply,other_routes_preserved=True)))
    if not apply:return
    # Verify application boundaries before making the new origin reachable.
    def probe(host,url):
        request=urllib.request.Request('http://127.0.0.1:8997'+url,headers={'Host':host})
        try:
            with urllib.request.urlopen(request,timeout=15) as r:return r.status,r.read()
        except urllib.error.HTTPError as e:return e.code,e.read()
    code,body=probe('arctrade.ai-ve.uk','/')
    if code!=200 or b'ArcTrade' not in body:raise RuntimeError('ArcTrade origin not ready')
    if probe('arctrade.ai-ve.uk','/api/leaderboard')[0]!=401:raise RuntimeError('Research auth boundary failed')
    if probe('arctrade.ai-ve.uk','/api/public/paper/kr')[0]!=200:raise RuntimeError('Public paper unavailable')
    code,body=probe('quantinsight.ai-ve.uk','/')
    if code!=200 or b'<body></body>' not in body:raise RuntimeError('Blank QuantInSight not ready')
    backup=vault/'ArcTrade/wiring'/str(time.time_ns());backup.mkdir(parents=True,mode=0o700)
    (backup/'before.json').write_text(json.dumps(dict(tunnel=before,dns=dns)))
    current=cf(path)
    if current['version']!=before['version']:raise RuntimeError('Concurrent tunnel change')
    cf(path,'PUT',dict(config=routed(before['config'])))
    for host,records in dns.items():
        row=records[0]
        cf(f'/zones/{ZONE}/dns_records/'+row['id'],'PATCH',dict(type='CNAME',name=host,
           content=TUNNEL+'.cfargotunnel.com',proxied=True,ttl=1))
    after=cf(path)
    if [r for r in after['config']['ingress'] if r.get('hostname') not in ROUTES]!=[r for r in before['config']['ingress'] if r.get('hostname') not in ROUTES]:
        raise RuntimeError('Unrelated routes changed; inspect backup')
    (backup/'receipt.json').write_text(json.dumps(dict(version=after['version'],routes=ROUTES)))
    print('Only the three requested hosts changed; no tunnel or collector restart')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');main(p.parse_args().apply)
