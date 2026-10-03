"""Model adapter with explicit protocol and response-shape checks.

PLANNING may run on a cloud model; COMPOSITION never does.

That split is the whole point. A planner is sent the question, the concept
catalog and candidate entity NAMES. A composer is sent the passages
themselves -- the narrative of a case, the complainant's account. Those are
not the same disclosure, so they do not get the same setting: cloud
planning is opt-in via EVIDENCE_PLANNER_CLOUD, and every other call stays
on the local endpoint no matter what that flag says.

No call to the legacy cloud-fallback client is permitted in this runtime.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import asyncio
import os
import re
import time
from uuid import uuid4
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class ModelUnavailable(RuntimeError):
    pass


class ModelOutputError(ValueError):
    def __init__(self,message,raw=None):
        super().__init__(message)
        self.raw=raw


def decode_object(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    try:
        obj = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ModelOutputError("Model returned malformed or truncated JSON") from exc
    if not isinstance(obj, dict):
        raise ModelOutputError("Model must return one JSON object")
    return obj


def request_preamble(contract, system: str) -> str:
    """The text one request carries before any payload.

    MEASURED: the local route sends `compact_schema` with compact separators
    and the cloud route sends the RAW schema with pretty ones -- 3,575
    characters more, on the one route that has a size limit. A budget
    computed from the local schema therefore under-measured every cloud
    request by exactly that much, and two repairs in a row went out over the
    limit and came back HTTP 413 in 0.25s.

    Both routes now serialise the same compacted schema the same way, so
    this measures what they actually send. The TEXT is returned rather than a
    length, because the caller budgets in TOKENS and Urdu costs far more
    tokens per character than English.
    """
    return system + json.dumps(compact_schema(contract.model_json_schema()),
                               separators=(',', ':')) + " " * 120


def compact_schema(schema):
    """Remove annotation overhead while preserving validation, names and GUIDANCE.

    `description` was stripped along with `title` and `examples`, and it does
    not belong with them. The eight descriptions on QuestionPlan are
    instructions to the planner, written against mistakes it actually makes:
    "NEVER a name -- use object_name for that", "Required for count_entities",
    "Do not substitute a filter or predicate_id". They were authored and then
    discarded on BOTH routes, so no model ever read them.

    Dropping the rest still removes 40% of the schema, which matters: the
    free tier allows 8,000 tokens a minute and one planning call spends most
    of it, so every thousand characters of filler costs a question.
    """
    if isinstance(schema,list): return [compact_schema(item) for item in schema]
    if not isinstance(schema,dict): return schema
    output={}
    for key,value in schema.items():
        if key in {'title','default','examples'}: continue
        if key in {'properties','$defs','definitions'}:
            output[key]={name:compact_schema(definition) for name,definition in value.items()}
        else: output[key]=compact_schema(value)
    return output


def _quota_wait(response) -> float | None:
    """How long the server said to wait -- AS IT SAID IT, not clamped.

    Both readings of this were lossy, and each one turned a legible limit
    into an illegible failure.

    MEASURED: the server answered `retry-after: 586` with the message "Rate
    limit reached ... on tokens per day (TPD): Limit 200000, Used 198233 ...
    Please try again in 9m45.36s". The header was clamped to 30 seconds, so
    the code waited half a minute against a ten-minute quota, exhausted its
    attempts and reported that the evidence service could not complete the
    request -- a daily budget reading exactly like a bug. The message parser
    could not have rescued it either: `([0-9.]+)s` does not match
    "9m45.36s", so the minutes were dropped and a ten-minute wait parsed as
    45 seconds.

    The true wait is returned. Deciding whether to sit it out belongs to the
    caller, which knows the deadline; a clamp here just hides the number
    from everyone.
    """
    header = response.headers.get("retry-after")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    try:
        message = str(response.json().get("error", {}).get("message", ""))
    except Exception:                                   # noqa: BLE001
        return None
    found = re.search(r"try again in (?:([0-9]+)m)?([0-9.]+)s", message)
    if not found:
        return None
    minutes, seconds = found.group(1), found.group(2)
    return float(minutes or 0) * 60 + float(seconds) + 0.5


def quota_exhausted(response) -> str | None:
    """The limit the server named, when it named a limit rather than a pace.

    "tokens per day" and "tokens per minute" are different facts and need
    different answers: one is waited out in seconds, the other means no
    further planning today. Reporting both as "could not complete this
    request" is what made an exhausted budget indistinguishable from a
    defect.
    """
    try:
        message = str(response.json().get("error", {}).get("message", ""))
    except Exception:                                   # noqa: BLE001
        return None
    found = re.search(r"\b(tokens|requests) per (day|minute|hour)\b", message, re.I)
    if not found:
        return None
    used = re.search(r"Limit\s+([0-9]+),\s*Used\s+([0-9]+)", message, re.I)
    detail = (" (%s of %s used)" % (used.group(2), used.group(1))) if used else ""
    return "%s per %s%s" % (found.group(1), found.group(2).lower(), detail)


def _retry_after(response) -> float | None:
    """Backwards-compatible alias; the wait is no longer clamped here."""
    return _quota_wait(response)


def seconds_from(text) -> float:
    """A Groq duration string ("577ms", "6.07s", "1m2.5s") as seconds.

    The milliseconds suffix is checked FIRST: a pattern that looks for
    minutes before milliseconds reads "577ms" as five hundred and seventy
    seven MINUTES, which is how a half-second wait became a nine-hour one.
    """
    text = str(text or "").strip().lower()
    if not text:
        return 0.0
    if text.endswith("ms"):
        try:
            return float(text[:-2]) / 1000
        except ValueError:
            return 0.0
    found = re.match(r"^(?:([0-9.]+)m)?(?:([0-9.]+)s?)?$", text)
    if not found:
        return 0.0
    minutes, value = found.groups()
    try:
        return float(minutes or 0) * 60 + float(value or 0)
    except ValueError:
        return 0.0


class _Cache:
    """Identical request, identical answer, no tokens spent.

    WHY

    A planning call costs most of a minute's token allowance, and the same
    questions get asked over and over -- rehearsing a demo, re-running a
    battery, a reader repeating themselves. MEASURED during a twenty-question
    run: the second question took 94 seconds, 85 of them waiting for the
    per-minute window to refill after the first had drained it.

    WHAT IS SAFE TO CACHE, AND WHY THIS IS

    The key is the WHOLE request: the contract, the system prompt, the
    payload, the model and the temperature. The payload carries the catalog,
    the resolved entity candidates and the retrieved passages -- so if the
    records change, the payload changes, the key changes, and the old entry
    is simply never looked up again. The cache cannot serve a stale answer
    about data that has moved; it can only serve the same answer to the
    same question about the same corpus.

    It also cannot leak across scopes. Two users with different case access
    get different catalogs and different candidates, so they get different
    keys. Nothing is shared that was not already identical.

    Only VALIDATED results are kept: a refusal, a timeout or a malformed
    reply is never remembered, because the next attempt may well succeed.

    IT IS IN MEMORY ON PURPOSE

    Case text lives in Postgres behind row-level security. Writing cached
    drafts to a file would put that same text somewhere with none of those
    controls, for a saving that only matters within one session anyway. So
    it lives in this process and dies with it.

    Entries are deep-copied in and out: the runtime mutates plans and
    results, and a shared object would let one question quietly alter
    another's answer.
    """

    def __init__(self, limit: int = 256):
        self._items: "OrderedDict[str, object]" = OrderedDict()
        self.limit = limit
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(contract, system, payload, model_name, temperature, route,
            endpoint="default") -> str:
        """Same request, to the same ENDPOINT.

        The endpoint belongs in the key because a different transport is a
        different source of truth. MEASURED: a test issues the identical
        request twice through two transports -- one that answers correctly
        and one that omits the required controls -- and asserts the second
        fails. Without the endpoint in the key the first answer was served
        to the second, and a test for a refusal saw a success.
        """
        blob = json.dumps({"c": contract.__name__, "s": system, "p": payload,
                           "m": model_name, "t": round(float(temperature or 0), 3),
                           "r": route, "e": endpoint},
                          sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key):
        item = self._items.get(key)
        if item is None:
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return item.model_copy(deep=True)

    def put(self, key, value) -> None:
        try:
            self._items[key] = value.model_copy(deep=True)
        except Exception:                                # noqa: BLE001
            return                                       # not copyable: not cached
        self._items.move_to_end(key)
        while len(self._items) > self.limit:
            self._items.popitem(last=False)


CACHE = _Cache(int(os.getenv("EVIDENCE_MODEL_CACHE_ENTRIES", "256")))


def cache_enabled() -> bool:
    return os.getenv("EVIDENCE_MODEL_CACHE", "true").strip().lower() == "true"


class _KeyPool:
    """Several API keys, so a rate-limited one does not stop the run.

    WHY

    MEASURED across a twenty-question battery on one key: three questions
    failed outright on 413/429 and one took 301 seconds, 85% of it waiting
    for a per-minute window to refill. One planning request costs about
    7,000 of the 8,000 tokens a minute allows, so the SECOND question in any
    minute is refused -- and a repair attempt is refused for the same
    reason, which is how a recoverable plan became a failed answer.

    Each key carries its own per-minute and per-day allowance, so a refused
    key is simply set aside until the server's own stated reset time and the
    next one serves immediately. The run waits only when every key is
    cooling, and then only until the soonest is free.

    WHAT IS NEVER RECORDED

    A key is identified in diagnostics by its POSITION, never its value.
    Diagnostics are written to traces, logs and run records, and a secret
    that reaches any of those has escaped.
    """

    def __init__(self):
        self._cooldown: dict[int, float] = {}
        self._next = 0

    @staticmethod
    def keys() -> list[str]:
        from src import config
        pooled = [k.strip() for k in (os.getenv("GROQ_API_KEYS") or "").split(",") if k.strip()]
        if pooled:
            return pooled
        single = getattr(config, "GROQ_API_KEY", None)
        return [single] if single else []

    def available(self, now: float) -> list[int]:
        return [i for i in range(len(self.keys()))
                if self._cooldown.get(i, 0.0) <= now]

    def take(self, now: float):
        """The next usable key, round-robin, or None when all are cooling."""
        keys = self.keys()
        if not keys:
            return None
        free = self.available(now)
        if not free:
            return None
        # Round-robin so load spreads instead of draining the first key.
        for offset in range(len(keys)):
            index = (self._next + offset) % len(keys)
            if index in free:
                self._next = (index + 1) % len(keys)
                return index
        return None

    def rest(self, index: int, seconds: float, now: float) -> None:
        self._cooldown[index] = now + max(0.0, seconds)

    def soonest(self, now: float) -> float:
        keys = self.keys()
        if not keys:
            return 0.0
        waits = [max(0.0, self._cooldown.get(i, 0.0) - now) for i in range(len(keys))]
        return min(waits) if waits else 0.0

    def status(self, now: float) -> dict:
        return {"keys": len(self.keys()), "available": len(self.available(now))}


KEYS = _KeyPool()


def cloud_planning_enabled() -> bool:
    """Whether PLANNING may leave this machine. Off unless switched on."""
    from src import config
    return (os.getenv("EVIDENCE_PLANNER_CLOUD", "false").lower() == "true"
            and bool(config.GROQ_API_KEY))


class LocalModel:
    def __init__(self, *, transport=None, priority='interactive'):
        if priority not in {'interactive','background'}: raise ValueError('Unknown model admission priority')
        self.transport = transport
        self.priority = priority
        self.diagnostics: list[dict] = []

    async def structured(self, contract: type[T], system: str, payload: dict,
                         *, generation: bool = False, deadline: float | None = None,
                         allow_cloud: bool = False, temperature: float = 0.0,
                         coerce=None) -> T:
        started = time.monotonic()
        diagnostic = {"contract": contract.__name__, "outcome": "started"}
        self.diagnostics.append(diagnostic)
        # ── AN IDENTICAL REQUEST HAS AN IDENTICAL ANSWER ──────────────────
        #
        # Keyed on the whole request, so a change in the records changes the
        # payload, changes the key, and the old entry is never consulted
        # again. A hit is RECORDED as a hit: a trace that showed a model call
        # which never happened would be a lie of exactly the kind this
        # system exists to prevent.
        from src import config
        route = "cloud" if (allow_cloud and cloud_planning_enabled()) else "local"
        model_name = config.GROQ_MODEL if route == "cloud" else "local"
        key = None
        if cache_enabled():
            key = CACHE.key(contract, system, payload, model_name, temperature,
                            route, "default" if self.transport is None
                            else "transport-%d" % id(self.transport))
            hit = CACHE.get(key)
            if hit is not None:
                diagnostic.update(outcome="validated", cached=True, route=route,
                                  model=model_name, seconds=0.0,
                                  cache_hits=CACHE.hits, cache_misses=CACHE.misses)
                return hit
        try:
            if allow_cloud and cloud_planning_enabled():
                result = await self._cloud(contract, system, payload, deadline=deadline,
                                           diagnostic=diagnostic, temperature=temperature,
                                           coerce=coerce)
            else:
                result = await self._structured(contract, system, payload, generation=generation,
                                                deadline=deadline, diagnostic=diagnostic,
                                                temperature=temperature, coerce=coerce)
            diagnostic["outcome"] = "validated"
            if key is not None:
                CACHE.put(key, result)
                diagnostic["cached"] = False
            return result
        except asyncio.CancelledError:
            diagnostic["outcome"] = "cancelled"
            raise
        except Exception as exc:
            if diagnostic["outcome"] == "started":
                diagnostic["outcome"] = type(exc).__name__
            diagnostic["error_type"] = type(exc).__name__
            raise
        finally:
            diagnostic["seconds"] = round(time.monotonic()-started, 3)

    async def _structured(self, contract, system, payload, *, generation, deadline, diagnostic,
                          temperature: float = 0.0, coerce=None):
        from src import config
        url = config.LOCAL_GEN_LLM_URL if generation and config.LOCAL_GEN_LLM_URL else config.LOCAL_LLM_URL
        name = config.LOCAL_GEN_LLM_MODEL if generation and config.LOCAL_GEN_LLM_URL else config.LOCAL_LLM_MODEL
        if not url:
            raise ModelUnavailable("The local model endpoint is not configured")
        # No deadline means the model is given the time it needs. The old
        # fallback of 60 seconds was a hard threshold wearing a default's
        # clothes: it cut off planning that was still working and reported
        # a transport timeout.
        remaining = (deadline - time.monotonic()) if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise ModelUnavailable("The model deadline expired")
        schema = compact_schema(contract.model_json_schema())
        prompt = system + "\nReturn one JSON object matching this schema. No prose or code fences.\n" + json.dumps(schema,separators=(',',':'))
        message = json.dumps(payload, ensure_ascii=False, default=str,separators=(',',':'))
        diagnostic.update(model=name, input_chars=len(prompt)+len(message),
                          temperature=temperature)
        protocol = os.getenv("EVIDENCE_LOCAL_PROTOCOL", "custom")
        budget = int(os.getenv("EVIDENCE_MODEL_OUTPUT_TOKENS", "4096"))
        strict_output = os.getenv("EVIDENCE_MODEL_STRUCTURED_OUTPUT", "false").lower() == "true"
        disable_thinking = os.getenv("EVIDENCE_MODEL_THINK_CONTROL", "false").lower() == "true"
        wrapper_v2 = os.getenv('EVIDENCE_MODEL_WRAPPER_V2','false').lower() == 'true'
        if wrapper_v2 and protocol != 'custom':
            raise ModelUnavailable('Wrapper v2 requires the custom gateway protocol')
        if protocol == "custom":
            body = {"model": name, "system": prompt, "prompt": message,
                    "temperature": temperature, "max_tokens": budget}
            if strict_output or wrapper_v2:
                body["json_schema"] = schema
            if disable_thinking or wrapper_v2:
                body["think"] = False
            if wrapper_v2:
                body.update(request_id=str(uuid4()),priority=self.priority,
                            timeout_seconds=min(600,max(.001,remaining if remaining is not None else 90)))
        elif protocol == "openai":
            body = {"model": name, "messages": [{"role": "system", "content": prompt},
                    {"role": "user", "content": message}], "temperature": temperature, "max_tokens": budget}
            if strict_output:
                body["response_format"] = {"type": "json_schema", "json_schema": {"name": contract.__name__, "schema": schema, "strict": True}}
            if disable_thinking:
                body["chat_template_kwargs"] = {"enable_thinking": False}
        elif protocol == "ollama":
            body = {"model": name, "messages": [{"role": "system", "content": prompt},
                    {"role": "user", "content": message}], "stream": False,
                    "options": {"temperature": temperature, "num_predict": budget}}
            if strict_output:
                body["format"] = schema
            if disable_thinking:
                body["think"] = False
        else:
            raise ModelUnavailable("Unsupported EVIDENCE_LOCAL_PROTOCOL")
        started = time.monotonic()
        try:
            # The DEADLINE is the budget. A flat 60s cap on top of it
            # abandoned the model mid-answer on questions that were simply
            # slower than average: "who is Ahmed and is he nominated in any
            # cases during the last 2 months" planned for longer than 60s
            # and was cut off as a transport_timeout, with 90s of the
            # caller's deadline still unspent.
            async with httpx.AsyncClient(
                    timeout=(max(5.0, remaining) if remaining is not None else None),
                    transport=self.transport) as client:
                key=os.getenv('EVIDENCE_MODEL_API_KEY','')
                headers={'Authorization':'Bearer '+key} if key else {}
                response = await client.post(url, json=body,headers=headers)
                diagnostic["http_status"] = response.status_code
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            diagnostic["outcome"] = ("transport_timeout" if isinstance(exc, httpx.TimeoutException)
                                     else "http_error" if isinstance(exc, httpx.HTTPStatusError)
                                     else "invalid_server_json" if isinstance(exc, ValueError)
                                     else "transport_error")
            # Do not disclose the private endpoint URL, prompt, or credentials.
            raise ModelUnavailable(f"Local model request failed ({type(exc).__name__})") from exc
        try:
            if not isinstance(data,dict):
                raise TypeError("Response envelope must be an object")
            if wrapper_v2:
                controls=data.get('effective_controls') or {}
                if controls.get('think') is not False or controls.get('schema') is not True:
                    diagnostic['outcome']='unsupported_controls'
                    raise ModelUnavailable('GPU wrapper did not confirm the required native controls')
                diagnostic['effective_controls']=controls
            if protocol == "openai":
                choice = data["choices"][0]
                raw, finish = choice["message"].get("content"), choice.get("finish_reason")
            elif protocol == "ollama":
                raw, finish = data["message"].get("content"), data.get("done_reason")
            else:
                raw, finish = data.get("response"), data.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            diagnostic["outcome"] = "protocol_error"
            raise ModelOutputError("Local server response does not match its configured protocol") from exc
        diagnostic.update(finish_reason=finish, empty=not bool(raw),
                          output_chars=len(raw) if isinstance(raw,str) else 0)
        usage = data.get("usage")
        diagnostic["usage"] = ({key:value for key,value in usage.items()
                                if key in {"prompt_tokens","completion_tokens","total_tokens"}
                                and isinstance(value,int)} if isinstance(usage,dict) else None)
        if finish in {"length", "max_tokens"}:
            diagnostic["outcome"] = "truncated"
            raise ModelOutputError("Model output was truncated; no partial plan was executed", raw=raw)
        if not isinstance(raw, str) or not raw.strip():
            diagnostic["outcome"] = "empty"
            raise ModelOutputError("Model returned no answer content")
        try:
            # A NAME IS EASIER TO COPY THAN A UUID, AND THIS IS WHERE IT
            # BECOMES ONE. See `label_to_id` in the runtime.
            decoded = decode_object(raw)
            if coerce is not None:
                decoded = coerce(decoded)
            return contract.model_validate(decoded)
        except ModelOutputError as exc:
            diagnostic["outcome"] = "malformed_json"
            raise ModelOutputError(str(exc), raw=raw) from exc
        except ValidationError as exc:
            diagnostic["outcome"] = "contract_error"
            errors = [{"type":e["type"], "loc":list(e["loc"]), "message":e["msg"][:400]}
                      for e in exc.errors(include_input=False)]
            diagnostic["validation_errors"] = errors
            raise ModelOutputError("Model response failed the typed contract: " + json.dumps(errors)[:2000],raw=raw) from exc

    async def _cloud(self, contract, system, payload, *, deadline, diagnostic,
                     temperature: float = 0.0, coerce=None):
        """Plan on the configured cloud model. Planning only -- see module docstring.

        MEASURED: qwen3:14b produced a different plan every run for the same
        question -- a name in a UUID field, a search with no text, output
        truncated mid-object, a request slower than any deadline. Each was
        made recoverable and the next appeared. The contract was not the
        problem; a 14B model emitting a typed plan with UUIDs was.
        """
        from src import config
        # The cloud route sent the RAW schema with pretty separators while
        # the local route sent a compacted one -- 3,575 characters more, on
        # the one route that has a token allowance. Same schema, same
        # meaning, fewer tokens, so more of the minute is left for the next
        # question.
        schema = compact_schema(contract.model_json_schema())
        prompt = (system + "\nReturn one JSON object matching this schema. "
                  "No prose or code fences.\n" + json.dumps(schema,separators=(',',':')))
        message = json.dumps(payload, ensure_ascii=False, default=str,separators=(',',':'))
        remaining = (deadline - time.monotonic()) if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise ModelUnavailable("The model deadline expired")
        diagnostic.update(model=config.GROQ_MODEL, route="cloud",
                          input_chars=len(prompt) + len(message))
        # MEASURED: gpt-oss-120b spends tokens REASONING before it answers,
        # and those count against max_tokens. At 64 the answer was empty and
        # Groq rejected the request outright with json_validate_failed and an
        # empty failed_generation -- which reads like a prompt fault and is
        # really a budget fault. The planner's own 4096 is the same trap one
        # size up, so planning gets its own, larger budget and asks for the
        # cheapest reasoning that still produces a plan.
        body = {"model": config.GROQ_MODEL,
                "messages": [{"role": "system", "content": prompt},
                             {"role": "user", "content": message}],
                "temperature": temperature,
                "max_tokens": int(os.getenv("EVIDENCE_PLANNER_OUTPUT_TOKENS", "8192")),
                "response_format": {"type": "json_object"}}
        effort = os.getenv("EVIDENCE_PLANNER_REASONING", "low").strip()
        if effort:
            body["reasoning_effort"] = effort
        try:
            async with httpx.AsyncClient(
                    timeout=(max(5.0, remaining) if remaining is not None else None),
                    transport=self.transport) as client:
                # HOW MANY TIMES A RATE REFUSAL IS WAITED OUT.
                #
                # Three attempts was a limit on patience, and the refusal is
                # not a failure -- it carries the exact wait. MEASURED: the
                # free tier allows 8,000 tokens a minute and one planning
                # call spends most of them, so a second question inside the
                # same minute is refused and the third attempt still lands
                # inside it. The run then reported that the service could not
                # complete the request, for a question it answers correctly
                # when it waits.
                attempts = max(1, int(os.getenv("EVIDENCE_PLANNER_RATE_ATTEMPTS", "8")))
                for attempt in range(attempts):
                    # A REFUSED KEY IS SET ASIDE; THE NEXT ONE SERVES NOW.
                    #
                    # Waiting out a window with other keys idle is the thing
                    # that turned a recoverable plan into a failed answer.
                    now = time.monotonic()
                    index = KEYS.take(now)
                    if index is None:
                        pause = KEYS.soonest(now)
                        room = None if deadline is None else (deadline - now) - 5
                        if pause <= 0 or (room is not None and pause > room):
                            diagnostic["outcome"] = "all_keys_cooling"
                            diagnostic.update(KEYS.status(now))
                            raise ModelUnavailable(
                                "Every planning key is rate limited; the soonest "
                                "is free in about %d second(s)." % round(pause))
                        await asyncio.sleep(min(pause, 70.0))
                        index = KEYS.take(time.monotonic())
                        if index is None:
                            diagnostic["outcome"] = "all_keys_cooling"
                            raise ModelUnavailable("Every planning key is rate limited")
                    pool = KEYS.keys()
                    # The POSITION is recorded, never the key: diagnostics
                    # reach traces, logs and stored runs.
                    diagnostic["key_index"] = index
                    diagnostic.update(KEYS.status(time.monotonic()))
                    response = await client.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        json=body,
                        headers={"Authorization": f"Bearer {pool[index]}"})
                    diagnostic["http_status"] = response.status_code
                    # WHAT IS LEFT OF THIS MINUTE'S ALLOWANCE.
                    #
                    # The server states it on every reply. Recorded so the
                    # caller can WAIT for the window before a repair instead
                    # of firing one into a budget it cannot fit -- the single
                    # commonest way a recoverable plan became a failed run.
                    try:
                        left = response.headers.get("x-ratelimit-remaining-tokens")
                        resets = response.headers.get("x-ratelimit-reset-tokens")
                        if left is not None:
                            diagnostic["tokens_left_this_window"] = int(float(left))
                        if resets:
                            diagnostic["window_resets_in"] = resets
                    except (TypeError, ValueError):
                        pass
                    # 413 IS A RATE CONDITION HERE, NOT A BAD REQUEST.
                    #
                    # MEASURED: two planning requests of IDENTICAL size,
                    # 23,734 characters -- the first refused 413 in 0.235s,
                    # the second accepted 200. So the refusal does not depend
                    # on how large the request is in absolute terms; it
                    # depends on how much of the per-minute token allowance
                    # is left when it arrives.
                    #
                    # Treated as a bad plan, the 413 consumed a rung of the
                    # repair ladder, and a rung APPENDS correction text --
                    # making the next request larger and the next refusal
                    # more likely. Retried as the rate condition it is, the
                    # same request succeeds and the ladder keeps its rungs
                    # for plans that are actually wrong.
                    if response.status_code not in (429, 413):
                        break
                    # MEASURED: the free tier allows 8000 tokens per minute
                    # and one planning call costs about 4000, so a second
                    # question inside a minute is refused outright. The
                    # refusal is not a failure -- it carries the exact wait:
                    # "Please try again in 6.0675s". Discarding that turned a
                    # six-second pause into "could not complete this request".
                    # A 429 states its own wait; a 413 does not, so it gets
                    # a short backoff that lets the allowance refill.
                    wait = _retry_after(response)
                    # Name the limit that was hit, so an exhausted budget is
                    # not reported as a malfunction. A daily budget and a
                    # per-minute pace need opposite responses from the reader.
                    limit = quota_exhausted(response)
                    if limit:
                        diagnostic["quota"] = limit
                        diagnostic["quota_resets_in_seconds"] = wait
                    if wait is None and response.status_code == 413:
                        wait = float(os.getenv("EVIDENCE_PLANNER_BACKOFF", "5")) * (attempt + 1)
                    diagnostic["rate_limited"] = diagnostic.get("rate_limited", 0) + 1
                    room = None if deadline is None else (deadline - time.monotonic()) - 5
                    # A PACE IS WAITED OUT; A BUDGET IS REPORTED.
                    #
                    # These are not the same condition. A per-minute pace
                    # refills in seconds and waiting is the correct response.
                    # A daily budget refills in ten minutes or ten hours, and
                    # idling there -- eight attempts at 586 seconds, which is
                    # what honest waits produced -- is not patience, it is a
                    # run that never returns and never says why.
                    #
                    # So the wait is sat out only while it is short enough to
                    # be a pace. Anything longer stops here and is reported by
                    # name, which is a decision the reader can see rather than
                    # a clamp that hides the number.
                    # Rest the key that was refused for as long as the server
                    # says, then try the next one WITHOUT sleeping -- the whole
                    # point of a pool is that another key is already free.
                    KEYS.rest(index, wait if wait is not None else 60.0,
                              time.monotonic())
                    if attempt < attempts - 1 and KEYS.available(time.monotonic()):
                        continue
                    sit_out = float(os.getenv("EVIDENCE_PLANNER_MAX_WAIT", "90"))
                    if (attempt == attempts - 1 or wait is None
                            or wait > sit_out
                            or (room is not None and wait > room)):
                        break
                    await asyncio.sleep(wait)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            diagnostic["outcome"] = ("transport_timeout"
                                     if isinstance(exc, httpx.TimeoutException)
                                     else "http_error"
                                     if isinstance(exc, httpx.HTTPStatusError)
                                     else "transport_error")
            # Never disclose the endpoint, the prompt or the key.
            # A quota is a fact worth stating. "The evidence service could
            # not complete this request" reads as a defect and sent me
            # looking for one; "tokens per day (198233 of 200000 used),
            # resets in about 10 minutes" is actionable and true.
            if diagnostic.get("quota"):
                raise ModelUnavailable(
                    "The planning model's %s quota is exhausted%s. No plan was "
                    "attempted and no factual zero was inferred."
                    % (diagnostic["quota"],
                       ("; it resets in about %d minute(s)"
                        % max(1, round((diagnostic.get("quota_resets_in_seconds") or 0) / 60)))
                       if diagnostic.get("quota_resets_in_seconds") else "")) from exc
            raise ModelUnavailable(f"Planner request failed ({type(exc).__name__})") from exc
        try:
            choice = data["choices"][0]
            raw, finish = choice["message"].get("content"), choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            diagnostic["outcome"] = "protocol_error"
            raise ModelOutputError("Planner response was not in the expected shape") from exc
        diagnostic.update(finish_reason=finish, empty=not bool(raw),
                          output_chars=len(raw) if isinstance(raw, str) else 0)
        if finish in {"length", "max_tokens"}:
            diagnostic["outcome"] = "truncated"
            raise ModelOutputError("Planner output was truncated", raw=raw)
        if not isinstance(raw, str) or not raw.strip():
            diagnostic["outcome"] = "empty"
            raise ModelOutputError("Planner returned no content")
        try:
            # A NAME IS EASIER TO COPY THAN A UUID, AND THIS IS WHERE IT
            # BECOMES ONE. See `label_to_id` in the runtime.
            decoded = decode_object(raw)
            if coerce is not None:
                decoded = coerce(decoded)
            return contract.model_validate(decoded)
        except ModelOutputError as exc:
            diagnostic["outcome"] = "malformed_json"
            raise ModelOutputError(str(exc), raw=raw) from exc
        except ValidationError as exc:
            # The same conversion the local path makes. Without it a cloud
            # plan that misses the contract raised a bare ValidationError,
            # which the runtime's repair loop does not catch: "Which cases
            # is Ahmed accused in?" failed outright instead of being
            # repaired, and reported that the service could not complete.
            diagnostic["outcome"] = "contract_error"
            errors = [{"type": e["type"], "loc": list(e["loc"]),
                       "message": e["msg"][:400]}
                      for e in exc.errors(include_input=False)]
            diagnostic["validation_errors"] = errors
            raise ModelOutputError(
                "Planner response failed the typed contract: "
                + json.dumps(errors)[:2000], raw=raw) from exc
