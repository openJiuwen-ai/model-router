# x-router

Routes a request to a model by how much work it needs.

A small classifier reads the conversation and labels it `SIMPLE`, `MEDIUM`,
`COMPLEX`, `RESEARCH` or `REASONING`. Anything at or below a configured
capability level stays on the local model; anything above it goes to the model
you mapped that tier to. The classifier runs in your own process.

x-router occupies the algorithm slot, so it works like any other algorithm here:
it returns a model id and your application makes the call.

## Install

The Python distribution is `jiuwen-model-router`; its import package remains
`openjiuwen`. It is a Rust extension built with maturin, so install it from a
checkout rather than from an index. Use a virtual environment: a system Python
on Debian or Ubuntu is marked externally managed and refuses to install into
itself.

```bash
python3 -m venv .venv && source .venv/bin/activate   # or: uv venv && source .venv/bin/activate
pip install maturin
maturin develop --extras x-router                    # builds the extension, pulls the extra
```

The `jiuwen-model-router[x-router]` extra pulls in torch and transformers. The classifier is not optional:
assembling a router without one is an error, and the built-in heuristic is a
fallback for a classifier that fails at request time, or a mode you select
explicitly with `enabled = false` — never a default.

## Quick start

```toml
# router.toml
algorithm = "x-router"

[state]
backend = "memory"

[targets]
models = ["local", "fast-cloud", "deep-cloud", "reasoning-cloud"]

[x-router]
local_capability_level = "MEDIUM"   # SIMPLE..REASONING, or "NONE"
local_model = "local"

[x-router.tier_models]
COMPLEX   = "fast-cloud"
RESEARCH  = "deep-cloud"
REASONING = "reasoning-cloud"

[x-router.classifier_model]
model_path = "/path/to/classifier"
device = "auto"
```

```python
from openjiuwen import x_router

router = x_router.build_router("router.toml")

selection = x_router.build_request(
    [{"role": "user", "content": "Compare Raft and Paxos for a five-node cluster."}],
    session_id="s-1",
    agent_id="my-agent",
)
decision = router.route_sync(selection)

decision.selected_model_id   # "deep-cloud"
decision.reasoning           # "x-router: rule=escalate_cloud tier=RESEARCH source=llm"
```

Call the model yourself, then report the outcome so the router can avoid a
target that is failing:

```python
from openjiuwen import Feedback, RoutingKey

router.report_sync(Feedback(RoutingKey("s-1", "my-agent"), decision.selected_model_id, "ok", 840))
```

`build_request` is not optional. It flattens message content into text, and the
protocol layer rejects a message whose `content` is a list of parts — the shape
most OpenAI-compatible clients send.

## How it decides

| Condition | Result | `rule` |
|---|---|---|
| tier ≤ `local_capability_level` | `local_model` | `within_local_capability` |
| tier above it | the tier's model | `escalate_cloud` |
| that model is excluded by state feedback | `local_model` | `target_excluded_degrade` |

`local_capability_level = "NONE"` disables the local tier: everything escalates,
and `local_model` survives only as a degrade target. A tier with no entry in
`tier_models` falls back to the `COMPLEX` entry, which is therefore required.

The comparison is inclusive: with `local_capability_level = "MEDIUM"`, a request
classified `MEDIUM` stays local.

Every selectable model must appear in `[targets] models`. A mismatch fails when
the router is assembled, not on a live request.

## Reading a decision

`reasoning` is a stable `key=value` line. Two independent facts are reported,
because a routing rule and a classifier problem are different things.

**`rule`** — which routing rule fired: `within_local_capability`,
`escalate_cloud`, `target_excluded_degrade`.

**`source`** — where the tier came from:

| Value | Meaning |
|---|---|
| `llm` | the classifier answered and its output parsed |
| `heuristic` | no classifier configured; heuristic mode is deliberate |
| `heuristic_fallback` | the classifier was unreachable or raised |
| `parse_failed` | the classifier answered, but not with a single tier label |
| `unavailable` | everything failed; defaulted to `MEDIUM` |

A rising rate of `parse_failed` or `heuristic_fallback` means the classifier
deployment is unhealthy. It is visible in routing telemetry without extra
plumbing, so it is worth alerting on.

## Failure behaviour

**No failure escalates.** A classifier that is missing, slow, broken or talking
nonsense degrades toward the local model; it never causes a request to be sent
to a more expensive one. The only error x-router raises is when every target has
been excluded, which surfaces as `RouterError::NoTarget` for the host to handle.

| Failure | Result |
|---|---|
| Classifier fails at request time | heuristic tier, `source=heuristic_fallback` |
| Output is not a single tier label | heuristic tier, `source=parse_failed` |
| Nothing works at all | `MEDIUM`, `source=unavailable` |
| Every target excluded | `RouterError::NoTarget` |

That fallback exists for a classifier that breaks while serving. A classifier
that never worked is a different problem, and it is caught earlier:
`build_router` loads the model as part of assembly, so a missing section, a
missing `model_path`, a bad path or a device the interpreter cannot use all
raise there rather than turning into a router that quietly stops escalating.

| Configuration | Result |
|---|---|
| Valid | classifier loaded during assembly |
| No `[classifier_model]` section | `ParamsError` |
| Section with no `model_path` | `ParamsError` |
| `enabled = false` | heuristic, deliberately |
| Bad path or unusable device | `EngineError` |

## Classifier

The classifier runs in your process. There is no server to start and no port to
open: classification is a function call.

```toml
[x-router.classifier_model]
enabled = true
model_path = "/path/to/classifier"
device = "auto"              # "cpu", "cuda", "cuda:1", …
dtype = "auto"               # "bfloat16", "float16", "float32"
max_input_tokens = 4096      # prompt ceiling enforced at tokenisation
max_tokens = 16              # a tier label needs a handful of tokens
```

`device = "auto"` resolves to `cuda:0` whenever CUDA is available, which on a
shared machine is rarely what you want — pin an index. `dtype = "auto"` is
bfloat16 on CUDA and float32 on CPU.

**The interpreter has to match the device.** A CUDA device needs a torch built
with CUDA support; mismatched, the model fails to load and `build_router`
raises. Decoding is greedy: the same request yields the same tier.

Single device only. There is no sharding, no memory-fraction setting and no
quantisation; a small classifier fits on one card, and anything that does not
would need all three.

### Bringing your own

`ComplexityBackend` is the injection point. Anything with
`classify(request) -> str` replaces the shipped implementation — a remote
classifier, a different model stack, a stub in tests. An injected backend wins
over the configured one:

```python
class MyBackend:
    def classify(self, request):
        return call_my_service(request.prompt)   # returns e.g. "COMPLEX"

router = x_router.build_router("router.toml", backend=MyBackend())
```

The protocol is synchronous, because the algorithm slot is called
synchronously.

## Bandit

Off by default. When enabled, x-router remembers how conversations turned out
and lets that history override the classifier: if requests like this one kept
scoring better on another tier, that tier is used. It only overrides with
enough similar history and a clear margin, and it needs the host to report how
each turn went.

```toml
[state]
backend = "x-router-bandit"         # required with the store table below

[x-router.bandit]
enabled = true                      # false = keep collecting outcomes, never override
min_neighbors = 5                   # similar past turns needed before acting
margin = 0.3                        # how much better another tier must score
lambda_c = 0.2                      # weight of cost: U = quality − lambda_c · cost / cost_ref_usd
cost_ref_usd = 0.005

[x-router.bandit.store]
top_k = 10
min_similarity = 0.5
```

The store needs both the `[state]` line and its own table (the kernel takes a
backend name but no parameters for it); `build_router` refuses one without the
other. Install `.[x-router-bandit]` for numpy.

### Scoring

The bandit learns only from turns that were scored: after each call, the host
has to say how the turn went (a quality in `[-1, 1]`, plus what it cost). There
are two ways to do that, and one judge model behind both.

#### With the service

Two calls. The host passes what it already has — the messages, the response,
the cost — and the service does the rest: it asks a judge to score the turn on
a worker thread after `report` returns, and files the result.

```python
from openjiuwen import x_router

svc = x_router.build_service("router.toml")     # router + store + judge from one profile

selection = svc.route(messages, session_id="s-1", agent_id="my-agent")
response = call_model(selection.selected_model_id, messages)
svc.report(selection,
           messages=messages, response_text=response.text, tool_calls=response.tool_calls,
           cost_usd=response.cost_usd, latency_ms=response.latency_ms, outcome="ok",
           session_id="s-1", agent_id="my-agent")
```

`report` files the call result immediately; scoring is scheduled only for a
successful call that came with `messages` and `response_text`. Report
`cost_usd = 0.0` for free tiers rather than omitting it — a tier with unknown
cost is never chosen while cost carries weight.

Scoring failures cannot be raised from `report`, so watch `svc.stats`:

```python
svc.stats
# {"submitted": 120, "settled": 117, "judge_failed": 3, "dropped_full": 0, "pending": 0,
#  "store": {"pending": 0, "closed": 117, "dropped": 0, "unknown": 0, "version": 0}}
```

| Counter | Rising means |
|---|---|
| `judge_failed` | the judge is not answering — key, quota, device |
| `dropped_full` | the judge is too slow for the traffic; the queue (256) drops rather than blocks |
| `store.dropped` | turns never scored within `pending_ttl_secs` |
| `store.unknown` | scores arrived for a decision the store never saw — no hint, or the wrong selection reported |

Pass `on_settle_error=callback` to `build_service` to hear about each failure,
or call `svc.settle(...)` synchronously to get the exception. `svc.close()`
drains the queue on shutdown. The worker count defaults to 1 for a local judge
and 4 for an API judge (`workers=`). Without `[x-router.bandit]` or a judge,
`build_service` still works and `report` files call results only.

#### By hand

Every step is also a public helper, for hosts with their own task queue — or
their own quality signal, in which case no judge is needed at all:

```python
router = x_router.build_router("router.toml")
params = x_router.build_params("router.toml")
judge = x_router.build_judge("router.toml")

hint = x_router.build_hint(messages, params)                 # ① ask the store for neighbours
selection = router.route_sync(x_router.build_request(messages, session_id="s-1", agent_id="my-agent"), hint)
# ... call the model ...
router.report_sync(Feedback.ok(selection, latency_ms, session_id="s-1", agent_id="my-agent"))

# ② later, wherever you like: judge the turn and report its quality
quality = x_router.Scorer(router, judge).settle(
    selection, messages, response_text, tool_calls=tool_calls, cost_usd=cost_usd,
    session_id="s-1", agent_id="my-agent")

# ... or report a quality you already have (a grader, a test suite)
router.report_sync(x_router.build_bandit_feedback(
    selection, {x_router.SERVED: (quality, cost_usd)}, session_id="s-1", agent_id="my-agent"))
```

`SERVED` means the tier the selection routed to; name other tiers to report
several at once. Quality is a signed score in `[-1, 1]`. To keep the store
across a Router rebuild, or to read its `stats`, build it yourself:
`store = x_router.build_store(cfg); router = x_router.build_router(cfg, state=store)`.

#### The judge model

Scores are averaged across turns, so every host has to measure the same way:
the package fixes the rubric and its parser, the host supplies the model. Two
backends ship, chosen by one table:

```toml
[x-router.judge_model]                  # weights in this process
kind = "local"
model_path = "/path/to/judge"           # a 1.7B+ instruct model; the 0.6B classifier is too small to judge
device = "cuda:3"
max_tokens = 64

# — or —

[x-router.judge_model]                  # any OpenAI-compatible /chat/completions endpoint
kind = "api"
base_url = "https://openrouter.ai/api/v1"
model = "z-ai/glm-4.7"
api_key_env = "OPENROUTER_API_KEY"      # the variable's name; the key stays out of the profile
timeout_secs = 30
max_tokens = 64
```

`build_judge` loads the weights or checks the key at start-up. A local judge
can reuse the classifier's loaded weights (`share_classifier = true`, then
`build_judge(cfg, classifier=params.backend)`) — fine for a demo, too small
for production. Both backends raise `JudgeError` when the model does not
answer, never a made-up score. Anything with `score(request) -> str` works in
their place.

The rubric scores one assistant turn on `task_progress`, `correctness` and
`grounding`, each in `[-1, 1]`. With `kind = "api"`, the transcript and the
response leave the machine.

### Reading a bandit decision

Three more keys on the reasoning line, present only when the bandit is on:

| Key | Values | Meaning |
|---|---|---|
| `tier_llm` | a tier | what the classifier said; `tier` is what was used |
| `bandit` | `override`, `same`, `ignore`, `cold` | evidence replaced the tier; agreed with it; pointed elsewhere but not past `margin` (or said nothing about the classifier's tier); too few neighbours |
| `neighbors` | integer | similar past turns that contributed |

`bandit=cold` on every request means the store is not being consulted — no
hint sent, or nothing scored yet. `bandit=override` marks a turn whose tier is
not the classifier's; a host training the classifier on its own decisions must
leave those out. `parse_reasoning(selection.reasoning)` returns the line as a
dict.

Rollback is free: remove `[x-router.bandit]`, or set `enabled = false` to keep
collecting without acting, and routing is exactly what it was.

[`examples/x_router_bandit.py`](../../../examples/x_router_bandit.py) runs the
whole loop with a mocked classifier, models and judge — a dozen turns of one
request family, printing the reasoning keys as the store warms up and the
first `override` appears. `pip install numpy` is all it needs.

## Integration notes

**`route_sync` blocks** for as long as classification takes. An async host must
not call it from a coroutine:

```python
decision = await asyncio.to_thread(router.route_sync, request)
```

**One algorithm name per configuration.** The algorithm registry is
process-global and keyed by name, and re-registering a name replaces what was
there. Two routers assembled in one process from profiles that both declare
`algorithm = "x-router"` will share whichever configuration was built last, with
no error. Give each its own name:

```toml
algorithm = "x-router-support"     # service A
algorithm = "x-router-coding"      # service B
```

Separate processes are unaffected.

**Weights are per process.** Each router that loads a classifier holds its own
copy. That is fine for a small model and does not amortise across many routers
on one machine.

## Configuration reference

| Key | Default | Meaning |
|---|---|---|
| `local_capability_level` | `"MEDIUM"` | Highest tier that stays local; `"NONE"` disables the local tier |
| `local_model` | `"local"` | Target for local routing and for degrades |
| `tier_models` | — | Tier → model id; a `COMPLEX` entry is required |
| `classifier_preview_chars` | `6000` | Conversation budget in the classifier prompt |
| `classifier_model.enabled` | `true` | `false` runs on the heuristic classifier |
| `classifier_model.model_path` | — | Weights; required unless `enabled = false` |
| `classifier_model.device` | `"auto"` | `"cpu"`, `"cuda"`, `"cuda:N"` |
| `classifier_model.dtype` | `"auto"` | `"bfloat16"`, `"float16"`, `"float32"` |
| `classifier_model.max_input_tokens` | `4096` | Prompt ceiling at tokenisation |
| `classifier_model.max_tokens` | `16` | Output budget |
| `bandit.enabled` | `true` | `false` collects outcomes but never overrides |
| `bandit.min_neighbors` | `5` | Neighbours required before the bandit acts |
| `bandit.margin` | `0.3` | Utility gap a challenger tier must exceed |
| `bandit.lambda_c` | `0.2` | Weight of cost in the utility |
| `bandit.cost_ref_usd` | `0.005` | Cost normaliser |
| `bandit.store.retriever_dim` | `4096` | Hashing-trick vector size |
| `bandit.store.max_entries` | `2000` | Closed records kept (FIFO); memory ≈ `max_entries × retriever_dim × 4 B` (2000 × 4096 ≈ 32 MB) |
| `bandit.store.top_k` | `10` | Neighbours returned per query (≤ 256) |
| `bandit.store.min_similarity` | `0.5` | Cosine floor for a neighbour |
| `bandit.store.forgetting_gamma` | `1.0` | Per-version discount on older records |
| `bandit.store.pending_ttl_secs` | `7200` | Unscored decisions expire after this |
| `bandit.store.exclusion_ttl_secs` | `300` | Lifetime of call-feedback exclusions |
| `judge_model.kind` | — | `"local"` or `"api"` |
| `judge_model.max_tokens` | `64` | Output budget for the rubric JSON |
| `judge_model.model_path` / `device` / `dtype` / `max_input_tokens` | as classifier | `local` only |
| `judge_model.share_classifier` | `false` | `local` only: reuse the classifier's loaded engine |
| `judge_model.base_url` / `model` | — | `api` only: OpenAI-compatible endpoint and model id |
| `judge_model.api_key_env` | `"OPENAI_API_KEY"` | `api` only: environment variable holding the key |
| `judge_model.timeout_secs` | `30` | `api` only |
| `judge_model.headers` | `{}` | `api` only: extra request headers |

The `[x-router]` table sits in the same profile as the router's own
configuration; the kernel ignores tables it does not recognise.

## Tests

### Unit tests

`maturin develop` does not install a test runner, so add one:

```bash
pip install pytest
pytest                                             # everything
pytest --ignore=tests/test_x_router_live.py        # skip the live ones outright
```

Without the second flag the live tests are not run either — they skip themselves
unless a model is configured, as described below.

`test_x_router.py` substitutes the classifier and runs anywhere;
`test_x_router_bandit.py` covers the override rule with fabricated neighbours,
`test_x_router_bandit_store.py` the store (skipped without numpy),
`test_x_router_judge.py` the scoring rubric and its backends, and
`test_x_router_service.py` the two-call loop; the tests that
go through the kernel skip themselves if the native extension is not built.
`test_x_router_live.py` is the exception — it loads real weights to check that a
model deploys, answers with a parseable tier, orders tiers by difficulty, and
decodes deterministically. It skips itself when no model is configured:

```bash
X_ROUTER_CLASSIFIER_MODEL=/path/to/classifier \
    pytest tests/test_x_router_live.py
X_ROUTER_CLASSIFIER_MODEL=/path/to/classifier X_ROUTER_CLASSIFIER_DEVICE=cuda:1 \
    pytest tests/test_x_router_live.py
```

Pointing the variable at a path that does not exist fails rather than skips: at
that point you have asked for the live tests, so a wrong path is a typo worth
seeing rather than an absence to tolerate.

### Example

[`examples/x_router_cli.py`](../../../examples/x_router_cli.py) routes requests
from the command line, so you can see what a profile does before wiring it into
anything. It defaults to
[`config/x-router-example.toml`](../../../config/x-router-example.toml), a
template whose `model_path` is a placeholder — point it at real weights, or
select your own profile with `XR_CONFIG`, before the first run:

```bash
python examples/x_router_cli.py "write a bash script to rotate logs"
python examples/x_router_cli.py "hi" "refactor this" "prove it by induction"
echo "a longer request" | python examples/x_router_cli.py
```

```text
[classifier /path/to/classifier device=cuda:0  ready in 9.2s]
  2126ms  local              x-router: rule=within_local_capability tier=SIMPLE source=llm
   165ms  fast-cloud         x-router: rule=escalate_cloud tier=COMPLEX source=llm
   224ms  reasoning-cloud    x-router: rule=escalate_cloud tier=REASONING source=llm
```

`XR_CONFIG` selects a profile and `XR_DEVICE` overrides the classifier device,
which is the quickest way to compare a tier map or a capability level without
editing anything. The first request is slower than the rest: it pays for CUDA
warm-up.
