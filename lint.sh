#!/bin/bash

echo "Running isort on" "$@"
isort "$@"

echo "Running black on" "$@"
python -m black "$@"

echo "Running flake8 on" "$@"
python -m flake8 "$@"

echo "Running mypy on" "$@"
python -m mypy "$@"

echo "Running pylint on" "$@"
python -m pylint "$@"

echo "Removing .mypy_cache"
rm -rf .mypy_cache/
