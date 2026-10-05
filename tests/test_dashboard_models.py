import io
import json

from refund_eval.dashboard import models


def test_model_catalog_filters_tools_and_caches_without_credentials(monkeypatch):
    calls=[]
    data={'data':[
        {'id':'example/tool-model', 'name':'Tool model', 'supported_parameters':['tools'],
         'architecture':{'output_modalities':['text']}},
        {'id':'example/no-tools', 'name':'Plain model', 'supported_parameters':[],
         'architecture':{'output_modalities':['text']}},
        {'id':'example/image', 'supported_parameters':['tools'],
         'architecture':{'output_modalities':['image']}},
    ]}
    def fetch(request, **kwargs):
        calls.append(request)
        return io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr(models,'_cached',None)
    monkeypatch.setattr(models,'_expires',0)
    monkeypatch.setattr(models.urllib.request,'urlopen',fetch)
    result=models.model_catalog()
    assert result['models']==[{'id':'openrouter/example/tool-model','name':'Tool model'}]
    assert models.model_catalog()==result and len(calls)==1
    assert calls[0].get_header('Authorization') is None


def test_catalog_outage_preserves_cached_choices_and_manual_entry(monkeypatch):
    cached={'models':[{'id':'openrouter/example/previous','name':'Previous'}],'error':None}
    monkeypatch.setattr(models,'_cached',cached)
    monkeypatch.setattr(models,'_expires',0)
    def fail(*args,**kwargs):
        raise OSError('offline')
    monkeypatch.setattr(models.urllib.request,'urlopen',fail)
    result=models.model_catalog()
    assert result['models']==cached['models']
    assert 'add an OpenRouter model ID' in result['error']
