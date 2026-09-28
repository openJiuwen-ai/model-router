# Classifier speed comparison

[`benchmark_classifier.py`](benchmark_classifier.py) sends the same 50 fixed
tasks to either the Transformers classifier or the in-process vLLM backend. Twelve
questions are longer to show the effect of input length. It prints each
question's character count and classification time, plus total, mean, and
median. Model loading and two warmup requests are excluded. No routed target
model is called, and neither backend starts an HTTP server.

Use the same available GPU for both runs, one process at a time. Replace
`GPU_INDEX` with its physical index; `CUDA_VISIBLE_DEVICES` maps it to `cuda:0`
for the Transformers classifier and makes it the only GPU visible to vLLM.

1. Run the in-process vLLM backend:

   ```bash
   CUDA_VISIBLE_DEVICES=GPU_INDEX python examples/benchmark_classifier.py vllm /path/to/classifier
   ```

2. After it exits, run Transformers on the same GPU:

   ```bash
   CUDA_VISIBLE_DEVICES=GPU_INDEX python examples/benchmark_classifier.py transformers /path/to/classifier
   ```

Pass the same classifier weights to both runs. vLLM uses its default
prefix-caching behavior, as it would in the router; these numbers measure
end-to-end `classify()` latency, not GPU kernel time alone.
