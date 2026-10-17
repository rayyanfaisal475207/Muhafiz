# Private GPU gateway

The inspected existing wrapper exposes five request fields and no native schema,
thinking or cancellation controls. This service preserves its `/generate`
response envelope and `/llm`/`/qalb` routes while forwarding native Ollama controls. It does not change
the configured servers or deploy itself.

Ollama's [generate API](https://docs.ollama.com/api/generate) documents `format`
as JSON/schema output, `think` controls and streamed generation. Verify `/api/show`
for the installed model's thinking support. An exposed field is not proof of
effective model behavior; run synthetic probes before sending case evidence.

Configure secrets through the serving environment:

```text
MUHAFIZ_OLLAMA_BASE_URL=http://127.0.0.1:11434
MUHAFIZ_GPU_GATEWAY_KEY=<private service credential>
MUHAFIZ_GPU_CAPACITY=1
MUHAFIZ_GPU_QUEUE_LIMIT=32
MUHAFIZ_LLM_MODEL=qwen3:14b
MUHAFIZ_QALB_MODEL=mtaimoorhassan/qalb-llm-urdu-improved
```

Run from the repository root on the GPU host:

```powershell
python -m uvicorn services.model_gateway.app:app --host 127.0.0.1 --port 8081 --workers 1
```

Use private networking or a private authenticated reverse proxy for remote API
workers. Do not expose this service to browsers. Use **one gateway process per
GPU capacity group**; multiple workers multiply the admission limit. Interactive
requests precede queued background requests; ordering is FIFO within priority.
Queue time counts against the request deadline. Limits are bounded and queue
overflow returns 503 with Retry-After. Do not route around the gateway to the
same GPU if you want its admission limit to cover ingestion and chat together.

The gateway closes the upstream HTTP stream on cancellation, disconnect and
timeout. Verify that the deployed native backend releases generation capacity
when that stream closes; client cancellation alone cannot prove GPU cancellation.
This service does not add cloud fallback. It reports effective forwarded controls
and preserves native completion reason and token counters.

After synthetic and cancellation verification, configure Muhafiz's local URL
to this gateway and opt into `EVIDENCE_MODEL_WRAPPER_V2`,
`EVIDENCE_MODEL_STRUCTURED_OUTPUT`, `EVIDENCE_MODEL_THINK_CONTROL` and the private
`EVIDENCE_MODEL_API_KEY`. Keep the previous configuration in the rollout archive.
Changing a model gateway does not certify the application's release gates.

## Deploy alongside the existing wrapper

Extract the deployment ZIP into a new directory on the GPU host. Keep the
existing wrapper running while verifying this service on port 8081:

```powershell
python -m pip install -r requirements-gateway.txt
python -m pytest tests/test_evidence_gateway.py -q
python -m uvicorn services.model_gateway.app:app --host 127.0.0.1 --port 8081 --workers 1
```

Set the serving environment variables listed above before starting. The test
suite uses a simulated native backend; it does not verify your installed models.
After checking real structured generation and cancellation, set these values in
Muhafiz's environment, using the private gateway address reachable by its backend:

```text
LOCAL_LLM_URL=http://<private-gateway-address>:8081/llm
LOCAL_GEN_LLM_URL=http://<private-gateway-address>:8081/qalb
EVIDENCE_LOCAL_PROTOCOL=custom
EVIDENCE_MODEL_WRAPPER_V2=true
EVIDENCE_MODEL_STRUCTURED_OUTPUT=true
EVIDENCE_MODEL_THINK_CONTROL=true
EVIDENCE_MODEL_API_KEY=<same private service credential>
```

The localhost listener needs a private proxy or tunnel for remote callers.
Restart Muhafiz only after verification and recording its previous configuration.
Rollback restores those previous URLs, flags and credential configuration.
