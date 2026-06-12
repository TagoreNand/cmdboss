# Contributing to CMDBoss

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

```bash
git clone https://github.com/YOUR_USERNAME/cmdboss.git
cd cmdboss
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python cmdboss.py          # starts with --reload for dev
```

## Making Changes

1. **Fork** the repo and create a branch: `git checkout -b feature/your-feature`
2. Make your changes with clear, focused commits
3. Follow [Conventional Commits](https://www.conventionalcommits.org/):
   - `feat:` new feature
   - `fix:` bug fix
   - `docs:` documentation only
   - `refactor:` code restructure (no behaviour change)
   - `chore:` tooling, config
4. Open a Pull Request with a clear description of what and why

## Code Style

- Python: follow PEP 8, use type hints
- Keep functions small and single-purpose
- Add docstrings to public functions and classes
- No commented-out dead code in PRs

## Reporting Issues

Use [GitHub Issues](https://github.com/YOUR_USERNAME/cmdboss/issues) with:
- A clear title
- Steps to reproduce
- Expected vs actual behaviour
- Environment (OS, Python version, Docker version)

## Questions

Open a [Discussion](https://github.com/YOUR_USERNAME/cmdboss/discussions) for general questions.
