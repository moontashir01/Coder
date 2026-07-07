"""Offline eval harness for Coder (roadmap Tier 2 #6).

A small golden-task suite that asserts *observable* outcomes (file created,
edit applied, answer contains a token, N files written). The harness logic is
unit-tested offline; the live run against Ollama is `python -m evals.run` — a
manual measuring stick to catch regressions when changing models or prompts.
"""
