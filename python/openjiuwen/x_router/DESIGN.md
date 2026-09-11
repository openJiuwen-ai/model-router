# x-router — design notes

Why the pieces are where they are. For using x-router, see [README.md](README.md).

## 1. Scope

x-router does one thing: classify a conversation into five tiers and pick a model
for the tier.

Deliberately excluded, and not to be added without a fresh design round:

| Excluded | Why |
|---|---|
| Privacy classification and redaction | Redaction rewrites payloads; this kernel does not carry payloads. It belongs to the host. |
| Turn-type rules (forcing follow-ups local) | An artifact of a training loop, not routing semantics. |
| Outcome memory, bandits, policy training | Later phases; they need protocol extensions that do not exist yet. |
| Decision caching, cost accounting | The host's concern. |

That exclusion list is what lets x-router run without any change to existing
kernel code: everything `decide` needs already travels in
`RouteRequest.messages`.

## 2. Module boundaries

```text
types.py       tiers, parameters, the classifier protocol
complexity.py  prompt, preview, parsing, heuristic, degradation
algorithm.py   the routing rule and the slot plugin
facade.py      configuration and assembly
classifier/    the classifier: engine and its backend adapter
```

Four modules along one axis — types, classification, decision, assembly.

The routing rule lives with the algorithm that is its only caller. An earlier
arrangement split it out into a `rules` module on the grounds that it was the
pure part, which produced a file whose two halves had no relationship to each
other. Splitting by responsibility beats splitting by category.

`facade.py` is the exception and earns its file by being disposable: it is the
only module that touches `Router`, and it exists to work around two kernel gaps
(§5). When those are fixed, the file is deleted rather than untangled.

## 3. Design decisions

### 3.1 A backend protocol with an implementation behind it

`ComplexityBackend` is the injection point, so the algorithm never hard-codes a
model stack and stays testable without weights. An implementation ships anyway,
because the classifier is part of this router rather than something each host
has to supply. An injected backend wins over the configured one.

The protocol is synchronous: the algorithm slot is called synchronously, so
there is no event loop to await on.

### 3.2 The classifier is required, not optional

A missing classifier used to mean heuristic mode. That is the wrong default:
every failure in this design degrades toward the local model, so a router whose
classifier never loaded routes everything locally, raises nothing, and looks
exactly like a healthy router with easy traffic. The bill arrives as quality
loss that no alert fires on.

So an absent section or a missing `model_path` raises during assembly, and
`build_router` loads the weights rather than deferring to the first request.
Heuristic mode stays reachable through `enabled = false`, where the operator has
said what they want.

The runtime fallback is unchanged and unrelated: a classifier that breaks while
serving must not fail requests, and `source=heuristic_fallback` makes that
visible in telemetry.

### 3.3 In-process, no server

Router and classifier deploy together, so a network hop between them would be
pure overhead — a round trip plus a failure mode that then has to be degraded
around.

The cost is that weights are per process and do not amortise across many routers
on one machine. If that becomes the constraint, the answer is a server in front
of the same engine and an HTTP backend behind the same protocol. It was removed
deliberately, not because it does not fit.

### 3.4 Strict output parsing

The classifier output must be exactly one label. Matching the first label found
anywhere in noisy output hides how often the classifier is not answering the
question; parsing strictly and recording `parse_failed` makes that rate visible.

This is calibrated for a model that emits bare labels reliably. A model that
tends to add punctuation or a preamble would push a large share of traffic to
the heuristic — safe, since the heuristic never escalates, but lossy. Watch
`parse_failed` in telemetry; tolerating trailing punctuation is the obvious
first relaxation.

### 3.5 One text view

The heuristic fallback and the preview builder share the same message-filtering
pass, so the two classification paths never disagree about which text they are
looking at.

### 3.6 Distinct codes for every path

Every degraded path has its own `source` code rather than a log line and a
default tier. A classifier outage and a classifier that has drifted are
different problems with different owners, and collapsing them into one code
makes both invisible.

### 3.7 Purity

Algorithms in this kernel are normally pure functions. x-router is not: `decide`
calls the classifier. That is intentional — the routing decision is what the I/O
is for — but it has two consequences worth stating.

`route()` blocks the calling thread for as long as classification takes. CPython
releases the GIL while waiting, so other threads are unaffected, but an async
host must not call `route_sync` from a coroutine.

**`check_purity()` must not be used on x-router.** It has no useful mode here.
Against a live classifier it fails intermittently, since serving stacks do not
all decode deterministically and two adjacent tiers can flip on a near tie —
which is exactly the case that decides whether a request escalates. Against a
stubbed backend it always passes and proves nothing. It also performs one real
inference per round. What it would have guarded is covered directly instead, by
a truth table over `decide_by_tier`.

## 4. Frozen inputs

The classifier is a stock model, not a fine-tune, so its inputs are not a
contract with a checkpoint. They are the levers that decide its behaviour, and
the reason two measurements are comparable:

1. the preview rules — filtering, window size, truncation strategy and limits;
2. the label vocabulary and the output parser;
3. the heuristic keyword sets and numeric thresholds;
4. the prompt template — a tunable, not a frozen input; see below.

Changing any of these silently makes two benchmark runs incomparable without
anything failing, so the preview is guarded by a hash assertion in the tests.

**The prompt deliberately is not.** A hash guard is worth its friction only
where a value can change without anyone meaning to change it. The prompt is a
literal constant: editing it is always deliberate and visible in the diff. The
preview is computed, and drifts from edits that look entirely local — the window
constant, the scaffolding filter, content flattening, the truncation helper —
each of which alters every classifier input without touching anything that looks
like classifier input. That asymmetry, not importance, decides which one gets a
hash. Prompt wording is in fact the larger lever, and §3.4 expects it to be
tuned; locking it would be friction on exactly the work that needs doing.

What the tests assert instead is the one prompt failure that is silent: losing
the `{content}` placeholder still produces a well-formed prompt, describing no
conversation at all.

If a fine-tune ever lands, these stop being a comparability aid and become a
hard contract with the checkpoint. The guards are already in place for that.

## 5. Kernel gaps

Three limitations shape the API. None blocks use; all three would simplify it.

### G1 — algorithms cannot receive configuration from the profile

`RouterProfile.algorithm` is a bare string, and Python algorithms must be
constructible with no arguments, so per-deployment parameters have nowhere to
live except class attributes, which are process-global.

*Workaround:* `facade.py` reads the `[x-router]` table and generates a
parameterised subclass at assembly time; subclass creation is what registers it,
so the generated name is what the profile refers to. This is also why two
routers in one process must use different algorithm names.

*Fix:* an `[algorithm.params]` table plus an optional `configure()` hook. Then
`facade.py` disappears.

### G2 — `Message` carries no `tool_calls`

The protocol conversion keeps `role` and `content` only. Tool-call depth is one
of the heuristic's signals, and the preview annotates tool calls; through this
protocol both are always empty.

A turn whose content is empty and whose only substance is a tool call therefore
arrives as an empty message, is dropped from the conversation view, and yields
an empty preview — so `classify` falls back to the heuristic, which sees nothing
either and returns `SIMPLE`. Tool-only turns are invisible to the classifier.

*Workaround:* none. Degradation is toward local, so it is safe but lossy.

### G3 — `content` must be a string

`content` is extracted as a string, so a message whose content is a list of
parts raises `TypeError` out of `route()`. That is the standard shape for
multimodal messages, for structured text parts, and for the normalised form
several client SDKs emit even for plain text.

The failure is worse than a crash: a host that catches exceptions and degrades
to local, as it should, silently stops routing while everything looks healthy.

*Workaround:* `build_request()` flattens list content before the request is
built, which is lossless for x-router since it consumes only text.

*Fix:* G2 and G3 together are a small, backward-compatible widening of
`Message`, separable from larger protocol work — they are simply a mismatch with
the message format the ecosystem already uses.

## 6. Testing strategy

| Layer | Needs | Covers |
|---|---|---|
| Rules | nothing | truth table over tiers × capability settings × exclusions; parameter validation |
| Complexity | nothing | preview rules, the preview hash, strict parsing, every degradation path |
| Classifier | nothing | engine contract, backend, lazy loading, no heavy imports at package import |
| Integration | the native extension | assembly, `route` → `report` → degrade, and G3 |
| Live | weights | the model loads, produces a parseable tier, orders tiers correctly, decodes deterministically |

Everything except the last two layers runs with no weights and no accelerator,
because the engine is injected rather than constructed inside the code under
test.

Two guards are worth knowing about:

**Heavy imports.** The kernel's discovery pass imports every module under this
package to find algorithms, so `import openjiuwen` reaches the engine module.
That is only harmless because `import torch` sits inside `load()`. A test checks
this in a fresh interpreter; moving that import to module scope would give the
whole kernel a multi-second import, including for callers who never touch
x-router.

**Not configured versus misconfigured.** The live tests skip when no model path
is set and fail when the path is set but wrong. Setting the variable is a
request to run them, so a bad path is a typo to surface rather than an absence
to tolerate — and by default pytest renders a skip and a "not applicable" the
same way.

## 7. Open items

- Tier coverage. A stock classifier may not use the whole range evenly; a model
  that rarely emits `MEDIUM` makes the local tier narrower in practice than the
  configuration says, which moves the local/cloud split and the cost with it.
  Measure the distribution on real traffic before quoting a split.
- Upstream: G2 and G3 as one small widening of `Message`.
- Upstream: G1 as `[algorithm.params]`, which removes `facade.py`.
- Upstream: let `check_purity` honour an algorithm that declares itself impure,
  rather than silently running it.
