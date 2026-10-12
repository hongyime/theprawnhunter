#!/bin/bash
# Development setup script

echo "Setting up development environment..."

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Install pre-commit hooks
echo "Installing pre-commit hooks..."
pip install pre-commit
pre-commit install

# Run validation
echo "Running startup validation..."
python scripts/deploy/validate_startup.py

# Run linter
echo "Running code quality checks..."
ruff check app/ --fix
ruff format app/

# Run tests
echo "Running tests..."
pytest tests/unit/ -v

echo ""
echo "✅ Development environment ready!"
echo ""
echo "Available commands:"
echo "  ruff check app/ --fix    # Lint and auto-fix"
echo "  ruff format app/         # Format code"
echo "  pytest tests/ -v         # Run tests"
echo "  mypy app/                # Type check"
echo "  pre-commit run --all     # Run all hooks"
