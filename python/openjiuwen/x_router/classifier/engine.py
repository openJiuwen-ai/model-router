"""Loads and runs the classifier model.

Used in process by :class:`~openjiuwen.x_router.classifier.backend.LocalBackend`
and by the optional local judge. This package does not provide an HTTP server.

Heavy imports happen inside methods. ``import openjiuwen`` must not pull in
torch, and this module is importable without the ``x-router`` extra installed so
that configuration errors surface before model loading does.
"""

from __future__ import annotations

import threading
from typing import Any, List, Optional

__all__ = ["ClassifierEngine", "EngineError"]

# Qwen3-class chat templates emit a reasoning block unless told otherwise. A
# classifier answering with one word has no use for it, and it would eat the
# short output budget.
_EMPTY_THINK = "<think>\n\n</think>\n\n"


class EngineError(RuntimeError):
    """Model could not be loaded or run."""


class ClassifierEngine(object):
    """A single-model, single-process text generator.

    Loading is lazy and happens once, under a lock: constructing an engine is
    cheap and safe at import time, while the weights are only touched when a
    classification is actually requested.
    """

    def __init__(
        self,
        model_path,
        device="auto",
        dtype="auto",
        max_input_tokens=4096,
    ):
        # type: (str, str, str, int) -> None
        if not model_path or not str(model_path).strip():
            raise EngineError("model_path is required")
        self.model_path = str(model_path)
        self.device = device
        self.dtype = dtype
        self.max_input_tokens = int(max_input_tokens)
        self._lock = threading.Lock()
        self._model = None  # type: Any
        self._tokenizer = None  # type: Any

    # -- loading -----------------------------------------------------------

    def load(self):
        # type: () -> None
        """Load weights and tokenizer. Idempotent; safe to call concurrently."""
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                import torch
                from transformers import AutoModelForCausalLM, AutoTokenizer
            except ImportError as exc:
                raise EngineError(
                    "the model-backed classifier needs extra packages; "
                    "pip install 'jiuwen-model-router[x-router]'"
                ) from exc

            try:
                tokenizer = AutoTokenizer.from_pretrained(self.model_path)
                # `dtype` is the transformers 5 spelling; on 4.x the argument was
                # `torch_dtype` and this name would be silently ignored. The
                # extra pins >= 5.0 for exactly that reason.
                #
                # Loading to host memory and moving afterwards, rather than
                # `device_map`, is deliberate: measured on a safetensors
                # checkpoint the two are identical in peak host RSS and in load
                # time, because the weights are memory-mapped and placed shard by
                # shard either way. `device_map` would only add an `accelerate`
                # dependency for no gain.
                model = AutoModelForCausalLM.from_pretrained(
                    self.model_path, dtype=self._resolve_dtype(torch)
                )
                model.to(self._resolve_device(torch))
                model.eval()
            except Exception as exc:
                raise EngineError(
                    "failed to load classifier from {0}: {1}".format(self.model_path, exc)
                ) from exc

            self._tokenizer = tokenizer
            self._model = model

    def _resolve_device(self, torch):
        # type: (Any) -> str
        if self.device != "auto":
            return self.device
        return "cuda" if torch.cuda.is_available() else "cpu"

    def _resolve_dtype(self, torch):
        # type: (Any) -> Any
        if self.dtype != "auto":
            return getattr(torch, self.dtype)
        return torch.bfloat16 if torch.cuda.is_available() else torch.float32

    @property
    def loaded(self):
        # type: () -> bool
        return self._model is not None

    # -- generation --------------------------------------------------------

    def build_prompt(self, messages):
        # type: (List[dict]) -> str
        """Render messages with the model's chat template, reasoning suppressed."""
        self.load()
        try:
            text = self._tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
        except TypeError:
            # Older templates do not accept enable_thinking; suppress explicitly.
            text = self._tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            if "<think>" not in text:
                text = text + _EMPTY_THINK
        return text

    def generate(self, prompt, max_new_tokens=16, temperature=0.0):
        # type: (str, int, float) -> str
        """Complete a prompt.

        ``temperature == 0`` decodes greedily rather than sampling at a small
        positive floor, so identical prompts return identical labels. Serving
        stacks vary on this, which is one reason the classifier runs here.
        """
        self.load()
        import torch

        inputs = self._tokenizer(
            prompt,
            return_tensors="pt",
            truncation=True,
            max_length=max(self.max_input_tokens - int(max_new_tokens), 1),
        )
        inputs = {key: value.to(self._model.device) for key, value in inputs.items()}
        prompt_length = inputs["input_ids"].shape[-1]

        kwargs = {"max_new_tokens": int(max_new_tokens), "do_sample": False}
        if temperature and float(temperature) > 0.0:
            kwargs = {
                "max_new_tokens": int(max_new_tokens),
                "do_sample": True,
                "temperature": float(temperature),
            }

        with torch.no_grad():
            output = self._model.generate(
                **inputs, pad_token_id=self._tokenizer.eos_token_id, **kwargs
            )
        completion = output[0][prompt_length:]
        return self._tokenizer.decode(completion, skip_special_tokens=True)

    def classify_text(self, text, max_new_tokens=16, temperature=0.0):
        # type: (str, int, float) -> str
        """Convenience path: wrap text as a user turn and complete it."""
        prompt = self.build_prompt([{"role": "user", "content": text}])
        return self.generate(prompt, max_new_tokens=max_new_tokens, temperature=temperature)
