from deploy.wire_arctrade import routed,ROUTES


def test_switch_preserves_unrelated_routes_and_is_idempotent():
    original={'ingress':[{'hostname':'other.example','service':'http://localhost:1'},
                {'hostname':'autocrypto.ai-ve.uk','service':'http://localhost:8996'},
                {'service':'http_status:404'}],'originRequest':{'connectTimeout':10}}
    result=routed(original)
    assert result['ingress'][0]==original['ingress'][0]
    assert result['ingress'][-1]==original['ingress'][-1]
    assert result['originRequest']==original['originRequest']
    assert len(result['ingress'])==5
    assert routed(result)==result
    assert original['ingress'][1]['service']=='http://localhost:8996'
