# Skill Name: Python Best Practices

## Description
Applies Python best practices including type hints, docstrings, error handling, and PEP 8 formatting.

## Trigger Keywords
python, py, type hint, docstring, pep8, best practices, clean code

## Instructions
When writing or reviewing Python code:
1. Always add type hints to function signatures
2. Write Google-style docstrings for all functions and classes
3. Use `pathlib.Path` instead of string paths
4. Prefer f-strings over `.format()` or `%` formatting
5. Use `dataclasses` or Pydantic models instead of raw dicts for structured data
6. Add `if __name__ == "__main__":` guards to all runnable scripts
7. Handle exceptions with specific exception types, not bare `except:`
