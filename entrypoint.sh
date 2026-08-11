#!/bin/sh
set -eu

mkdir -p /app/data /models
chown -R appuser:appuser /app/data /models

exec gosu appuser "$@"
