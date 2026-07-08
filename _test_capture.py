import litellm, httpx, json

_orig = httpx.Client.send
captured = []
def _capture(self, req, **kw):
    captured.append({"url": str(req.url), "body": req.content.decode("utf-8","replace") if req.content else None})
    return _orig(self, req, **kw)
httpx.Client.send = _capture

try:
    litellm.completion(model="openai/pool-deepseek-v4-pro", messages=[{"role":"user","content":"hi"}], max_tokens=10, api_key="__QCLAW_AUTH_GATEWAY_MANAGED__", api_base="http://127.0.0.1:19000/proxy/llm", extra_headers={"x-agent-id":"main"})
except Exception as e:
    pass

if captured:
    b = json.loads(captured[0]["body"])
    print("=== KEYS ===", list(b.keys()))
    print("=== BODY ===", json.dumps(b, indent=2, ensure_ascii=False))
