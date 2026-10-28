## What

<!-- What changed and why. -->

## How to try

```bash
pip install -e ".[dev]"
pytest
meet demo
```

## Checklist

- [ ] Tests pass with no API keys
- [ ] No secrets, join tokens, or real briefs in the diff
- [ ] README.md and README.zh-CN.md updated if behavior changed
- [ ] New provider or voice id is config / registry, not a hardcoded branch in the orchestrator
