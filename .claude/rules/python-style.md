# Python Docstrings and Type Hints

## Docstrings

All Python functions, classes, and methods MUST use Google-style docstrings with structured sections. One-liner
docstrings with no information about inputs, outputs, or exceptions are not acceptable.

### Required sections

- **Summary line**: A single imperative sentence describing what the function does.
- **Args**: Every parameter, its type, and a brief description. Omit only for zero-parameter functions.
- **Returns**: The return type and what it represents. Omit only for functions that return `None` with no meaningful
  side effect description needed.
- **Raises**: Every exception the function explicitly raises. Omit only if the function raises nothing.

### Optional sections

- **Examples**: Include when usage is non-obvious or the function has complex input/output shapes.
- **Note / Warning**: Include when there are important caveats (e.g., mutates input, not thread-safe).

### Example

```python
def fetch_changes(source_url: str, since: datetime) -> list[Change]:
  """Fetch changes from a monitored source since the given timestamp.

  Args:
      source_url: The URL of the source to check for changes.
      since: Only return changes after this timestamp.

  Returns:
      A list of Change objects ordered by timestamp ascending.

  Raises:
      SourceUnreachableError: If the source cannot be contacted.
  """
```

## Type Hints

All Python code MUST use type hints. Specifically:

- **Function signatures**: Annotate every parameter and the return type. No exceptions.
- **Class attributes**: Annotate in `__init__` or as class-level annotations.
- **Module-level variables**: Annotate when the type is not obvious from the right-hand side (e.g., annotate `registry:
  dict[str, Handler] = {}` but `count = 0` is fine without).
- **Collections**: Use generic types (`list[str]`, `dict[str, int]`) rather than bare `list` or `dict`.
- **Optional values**: Use `X | None` (union syntax) rather than `Optional[X]`.
- **Avoid `Any`**: Use `Any` only as a last resort. Prefer `object` or a `Protocol` for indeterminate types.
