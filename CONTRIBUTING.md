# Contributing to agy-telegram

Thank you for your interest in contributing to **agy-telegram**! We welcome bug reports, feature suggestions, documentation improvements, and code contributions.

---

## 🛠️ Development Setup

1. **Fork and Clone the Repository**:
   ```bash
   git clone https://github.com/kappino/agy-telegram.git
   cd agy-telegram
   ```

2. **Create a Virtual Environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install Dependencies**:
   ```bash
   pip install -e ".[dev]"
   ```

---

## 🧪 Running Tests & Linting

Before opening a pull request, ensure all tests and linters pass cleanly:

```bash
# Run test suite
pytest tests/ -v

# Or using Python's standard unittest runner
python3 -m unittest discover -s tests -v

# Code formatting and linting
ruff check .
ruff format --check .
```

---

## 📌 Coding Standards & Guidelines

- **Decoupled & Platform-Agnostic**: Avoid hardcoded system paths (such as `/root/` or system-specific commands). All configurations must come from standard discovery paths (`config.toml`, `.env`, or environment variables).
- **Asynchronous Architecture**: Network and I/O operations (Telegram API, file tailing, socket listeners) must use `asyncio`. Avoid blocking calls in the event loop.
- **Safety First**: Never bypass security prompts by default. The Human-in-the-Loop interactive approval mechanism must remain secure and deterministic.
- **Type Annotations**: Add type hints for new functions and classes (PEP 484 / PEP 561).

---

## 🔄 Pull Request Process

1. Create a feature branch from `main`:
   ```bash
   git checkout -b feature/my-new-feature
   ```
2. Commit your changes with clear, descriptive commit messages.
3. Add or update unit tests covering your changes.
4. Ensure the test suite passes.
5. Push to your fork and submit a Pull Request against the `main` branch.

---

## 🔒 Security Disclosures

If you discover a security vulnerability or critical issue, please do **not** open a public issue. Instead, report it privately to the maintainers or via GitHub Security Advisories.
