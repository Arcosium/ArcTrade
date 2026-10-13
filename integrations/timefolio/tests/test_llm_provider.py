import pytest

from web import llm


def test_missing_remote_key_is_rejected(monkeypatch):
    monkeypatch.setattr(llm.config, "OPENAI_API_KEY", "")
    with pytest.raises(llm.LLMConfigError):
        llm._resolve("openai")


def test_auto_selects_configured_provider_without_exposing_key(monkeypatch):
    monkeypatch.setattr(llm.config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(llm.config, "GEMINI_API_KEY", "configured-secret")
    monkeypatch.setattr(llm.config, "DEEPSEEK_API_KEY", "")
    name, _base, key, _model = llm._resolve("auto")
    assert name == "gemini"
    assert key == "configured-secret"
    assert "configured-secret" not in json_text(llm.provider_status())


def json_text(value):
    return str(value)


def test_local_system_prefix_precedes_changing_input(monkeypatch):
    import json
    sent=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self):return json.dumps({'choices':[{'message':{'content':'{}'}}]}).encode()
    monkeypatch.setattr(llm.urllib.request,'urlopen',lambda request,**kw:sent.append(json.loads(request.data)) or Response())
    for text in ('전략 A','전략 B'):llm.complete(text,provider='local',system='고정된 규칙표',json_mode=True)
    assert sent[0]['messages'][0]==sent[1]['messages'][0]=={'role':'system','content':'고정된 규칙표'}
    assert sent[0]['messages'][-1]!=sent[1]['messages'][-1]
