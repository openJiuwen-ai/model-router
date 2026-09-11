# x-router

Routes a request to a model by how much work it needs.

A small classifier reads the conversation and labels it `SIMPLE`, `MEDIUM`,
`COMPLEX`, `RESEARCH` or `REASONING`. Anything at or below a configured
capability level stays on the local model; anything above it goes to the model
you mapped that tier to. The classifier runs in your own process.

x-router occupies the algorithm slot, so it works like any other algorithm here:
it returns a model id and your application makes the call.

## Install

`openjiuwen` is a Rust extension built with maturin, so install it from a
checkout rather than from an index. Use a virtual environment: a system Python
on Debian or Ubuntu is marked externally managed and refuses to install into
itself.

```bash
python3 -m venv .venv && source .venv/bin/activate   # or: uv venv && source .venv/bin/activate
pip install maturin
maturin develop --extras x-router                    # builds the extension, pulls the extra
```

The extra pulls in torch and transformers. x-router classifies with a model, and
the model is not optional: assembling a router without one is an error.

There is a keyword and structure heuristic behind it, but it is a **fallback**,
not a mode you drift into. It takes over when a configured classifier fails at
request time, and it can be selected deliberately with `enabled = false` — never
by omission.

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
with CUDA support; mismatched, the model fails to load and routing degrades to
the heuristic.

`build_router` loads the weights, so assembly is where a broken deployment
surfaces. Decoding is greedy: the same request yields the same tier.

Single device only. There is no sharding, no memory-fraction setting and no
quantisation; a small classifier fits on one card, and anything that does not
would need all three.

`enabled = false` runs on the heuristic classifier. Omitting the section does
not — that is an error, because a router that silently never escalates is
indistinguishable from a healthy one.

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

`test_x_router.py` substitutes the classifier and runs anywhere; the tests that
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
