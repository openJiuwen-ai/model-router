"""Time 50 complexity-classifier tasks using in-process Transformers or vLLM.

Run once for each backend. Only classify() is timed: no target model is called.
Set CUDA_VISIBLE_DEVICES to select the physical GPU for either run.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python"))

from openjiuwen.x_router import (  # noqa: E402
    LocalBackend,
    VllmBackend,
    build_classifier_request,
    parse_complexity,
)

# Ten prompts in each intended work category. These are classification inputs;
# they do not call the models that x-router might select.
TASKS = [
    ("simple", "Can you say hi?"),
    ("simple", "What is 2 + 2?"),
    ("simple", "What is one synonym for quick?"),
    ("simple", "What is the capital of France?"),
    ("simple", "What color do red and blue make when mixed?"),
    ("simple", "What day comes after Tuesday?"),
    ("simple", "How many days are in a week?"),
    ("simple", "What is 10 minus 3?"),
    ("simple", "What is the Spanish word for hello?"),
    ("simple", "What is the plural of mouse?"),
    (
        "medium",
        "Can you summarize this incident note in three bullets? At 09:10 the checkout API "
        "began returning errors for about six percent of requests. The application had "
        "recently changed its retry policy from one retry to three, each with a fixed "
        "delay. The payment provider was slow but not fully unavailable. Retries caused "
        "the number of simultaneous outbound calls to rise, which exhausted the HTTP "
        "connection pool. Operators restored the previous policy at 09:35 and error "
        "rates returned to normal within five minutes. No duplicate charges were found "
        "in the sampled transaction logs. Include the trigger, user impact, and fix, "
        "without proposing any new architecture."
    ),
    ("medium", "Can you write a Python function that validates an email address?"),
    ("medium", "Can you convert these names to lowercase and remove duplicates: Ada, Grace, ADA?"),
    (
        "medium",
        "Can you rewrite this paragraph for clarity while preserving every concrete fact? "
        "The synchronization process has a tendency to fail sometimes when a device "
        "returns to the network after being disconnected. If the device has more than "
        "five hundred pending updates, it sends them in batches of one hundred. A batch "
        "is retried twice after a timeout of ten seconds. The user may see an old value "
        "until all batches finish. The current wording mixes the cause, the visible "
        "effect, and the retry policy, so reorganize it into a short paragraph that a "
        "support engineer can paste into a status update."
    ),
    ("medium", "How would you parse a CSV file with Python's csv module?"),
    ("medium", "Can you calculate a 15 percent tip on a 48 dollar bill?"),
    (
        "medium",
        "Can you draft a short email asking to reschedule a project review? The meeting "
        "is currently on Thursday at 15:00 UTC, but I have a customer incident at the "
        "same time. Suggest Friday between 13:00 and 16:00 UTC, or ask the group for "
        "another slot next week. The recipients are my manager and two teammates, so "
        "the tone should be direct and courteous. Mention that the agenda and document "
        "are ready, that the change is only about scheduling, and that I will send an "
        "updated calendar invite once everyone replies. Keep the email under 120 words."
    ),
    ("medium", "How do HTTP status codes 404 and 500 differ?"),
    ("medium", "Can you turn the pairs name=Ada and age=36 into a JSON object?"),
    ("medium", "Can you explain what this Python expression does: sorted(set(names))?"),
    (
        "complex",
        "How would you design retries and backpressure for a distributed task queue used "
        "by a billing service? Producers may enqueue forty thousand jobs per minute, "
        "workers call two external providers, and either provider can become slow for "
        "several minutes. Jobs must not be lost, and processing the same job twice must "
        "not charge a customer twice. Today every failure is retried immediately, which "
        "causes a backlog and makes the healthy provider slow as well. Describe the queue "
        "topology, worker concurrency limits, retry schedule, dead letter handling, "
        "idempotency keys, and the metrics operators need during an incident. Include "
        "how you would roll out the change without draining or deleting existing jobs, "
        "and explain what should happen when only one provider is degraded."
    ),
    ("complex", "How would you debug an intermittent timeout across three services and propose a fix and rollout plan?"),
    (
        "complex",
        "How would you plan an API migration from a single REST endpoint to separate "
        "read and write endpoints? Four web clients, two mobile releases, and a partner "
        "integration use the old endpoint. Mobile users may remain on an old release for "
        "months, while the partner deploys only once per quarter. The new write endpoint "
        "requires an idempotency key and returns an operation ID instead of the completed "
        "record. Some clients currently assume writes are synchronous. Give a staged "
        "compatibility plan, a versioning strategy, telemetry for tracking adoption, "
        "contract tests, and a rollback procedure. Address how reads behave while a "
        "write is still pending and how long the old contract should remain available."
    ),
    ("complex", "How would you design an idempotent payment API with retries and auditing?"),
    ("complex", "How would you diagnose a race condition between queue workers and a database transaction?"),
    ("complex", "How would you plan a zero downtime database schema migration for a service with several clients?"),
    ("complex", "How would you design an authentication system with account recovery, token rotation, and audit logs?"),
    ("complex", "How would you investigate a memory leak across a gateway and two worker services?"),
    (
        "complex",
        "How would you compare architecture options for an offline first field service "
        "app? Technicians edit customer notes, attach photos, and mark jobs complete on "
        "devices that can be offline for an entire day. Two technicians may update the "
        "same job before either reconnects. Completion status must never silently move "
        "backward, but notes can be merged when both edits are retained. Photos are large "
        "and the app sometimes has only a slow cellular connection. Compare at least two "
        "sync designs, explain conflict resolution for each data type, and show how you "
        "would test interrupted uploads, duplicate requests, and device clock skew. End "
        "with a recommendation and a migration path from the current online only app."
    ),
    ("complex", "How would you develop a rollout and rollback plan for replacing a message broker in production?"),
    (
        "research",
        "Can you compare current approaches to retrieval augmented generation using "
        "primary papers and benchmark reports? I need to understand the evidence for "
        "dense retrieval, sparse retrieval, hybrid retrieval, and reranking when the "
        "corpus contains both short documentation pages and long technical manuals. "
        "Please distinguish improvements in retrieval recall from improvements in final "
        "answer accuracy. Note which evaluations use private datasets, which release "
        "code or data, and whether reported gains hold when documents change frequently. "
        "I am interested in latency and indexing cost as well as quality. Synthesize "
        "where the sources agree, where they disagree, and which open questions would "
        "matter before choosing a design for a production support assistant."
    ),
    ("research", "What does the literature on battery degradation say, and where do findings conflict?"),
    ("research", "How do recent database indexing methods compare across sources?"),
    (
        "research",
        "What do recent primary studies say about the relationship between sleep "
        "duration and memory in adults? Please compare experimental sleep restriction "
        "studies with observational cohorts, and separate immediate recall from longer "
        "term retention. I would like to know how sample size, age, baseline health, "
        "measurement method, and follow-up period affect the conclusions. If the "
        "literature has conflicting results, identify whether differences in design or "
        "population could explain them. Use original studies where possible and avoid "
        "treating a correlation as proof of causation. End with what the evidence can and "
        "cannot support for a typical healthy adult."
    ),
    ("research", "How does the evidence from clinical trials of two treatments compare, and what are the study limitations?"),
    ("research", "What do recent papers on small language model evaluation say, and what questions remain open?"),
    ("research", "What do public datasets on urban heat reveal across cities?"),
    ("research", "What do primary sources report about recent advances in solid state batteries?"),
    (
        "research",
        "Can you compare published benchmarks for vector databases and assess how "
        "trustworthy their numbers are? Look for primary benchmark reports that disclose "
        "the dataset, vector dimension, recall target, index build settings, update "
        "frequency, hardware, and query concurrency. Some reports optimize for maximum "
        "throughput while others report single query latency; please keep those results "
        "separate. Examine whether filtering, deletes, and hybrid keyword queries were "
        "included. Identify where a vendor measured its own product and where an "
        "independent team reproduced the result. Then explain what comparison would be "
        "fair for a ten million document collection with daily updates and strict p95 "
        "latency requirements."
    ),
    ("research", "Where does research on software defect prediction show conflicting results?"),
    (
        "reasoning",
        "Can you prove that the square root of 2 is irrational using a contradiction? "
        "Start by assuming it equals a fraction a/b in lowest terms with positive "
        "integers a and b. Show explicitly why a squared being even forces a to be "
        "even, then use the same argument for b. Explain precisely why this contradicts "
        "the lowest terms assumption. I want every inference justified, including the "
        "parity lemma, rather than a one sentence appeal to a familiar theorem."
    ),
    ("reasoning", "Can you prove by induction that the sum of the first n odd numbers is n squared?"),
    ("reasoning", "Can you derive and solve a recurrence for binary strings with no consecutive ones?"),
    ("reasoning", "Can you prove that there are infinitely many prime numbers?"),
    ("reasoning", "Can you derive a closed form for the sum of the first n squares?"),
    (
        "reasoning",
        "Does a greedy coin change algorithm always use the fewest coins when the "
        "available denominations are 1, 3, and 4? The algorithm repeatedly takes the "
        "largest coin no greater than the remaining amount. Give either a proof for all "
        "nonnegative amounts or a concrete counterexample with a better solution. Then "
        "derive a correct dynamic programming recurrence for the minimum coin count, "
        "state its base case, and justify why considering the last coin gives an optimal "
        "answer. Explain how the conclusion changes, if at all, when the amount is zero."
    ),
    ("reasoning", "Can you prove or disprove that every connected graph with n vertices has at least n minus 1 edges?"),
    ("reasoning", "Can you derive the probability of exactly two heads in five fair coin flips from first principles?"),
    ("reasoning", "Can you prove by contradiction that no smallest positive rational number exists?"),
    (
        "reasoning",
        "Can you derive and justify an algorithm for detecting a cycle in a directed "
        "graph with n vertices and m edges? Describe the state kept for each vertex "
        "during a depth first search, and explain why an edge to a vertex currently on "
        "the recursion stack proves that a directed cycle exists. Also explain why an "
        "edge to a fully processed vertex does not prove the same thing. Give a proof "
        "that the algorithm finds a cycle whenever one exists, analyze time and space "
        "complexity, and say how to return the vertices of one cycle instead of only "
        "returning a Boolean result."
    ),
]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", choices=("transformers", "vllm"))
    parser.add_argument("model_path", help="path to the complexity-classifier weights")
    args = parser.parse_args(argv)

    if args.backend == "transformers":
        backend = LocalBackend(
            model_path=args.model_path,
            device="cuda:0",
            dtype="bfloat16",
            max_input_tokens=4096,
            max_tokens=16,
        )
    else:
        backend = VllmBackend(
            model_path=args.model_path,
            dtype="bfloat16",
            max_model_len=4096,
            gpu_memory_utilization=0.2,
            max_tokens=16,
        )
    backend.warmup()  # model loading is not timed for either backend

    requests = [
        build_classifier_request([{"role": "user", "content": task}], 6000)
        for _kind, task in TASKS
    ]
    for _ in range(2):
        backend.classify(requests[0])  # warm up inference and fail early if unavailable

    print("{0} classifier: {1} tasks".format(args.backend, len(TASKS)))
    times_ms = []
    for index, ((kind, task), request) in enumerate(zip(TASKS, requests), 1):
        started = time.perf_counter_ns()
        answer = backend.classify(request)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        times_ms.append(elapsed_ms)
        try:
            label = parse_complexity(answer).name
        except ValueError:
            label = "INVALID: {0!r}".format(answer)
        print("{0:2}. {1:<9} {2:4} chars  {3:8.1f} ms  {4}".format(
            index, kind, len(task), elapsed_ms, label
        ))

    print("total: {0:.1f} ms".format(sum(times_ms)))
    print("mean: {0:.1f} ms/task; median: {1:.1f} ms/task".format(
        statistics.mean(times_ms), statistics.median(times_ms)
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
