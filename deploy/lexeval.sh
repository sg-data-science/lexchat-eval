#!/bin/sh
# The container's commands: docker compose ... run --rm eval <command> [options]
set -eu
cd /app

command="${1:-help}"
[ $# -gt 0 ] && shift

case "$command" in
  check)
    exec python -m lex_eval.eval_run --check ;;
  run)
    exec python -m lex_eval.eval_run "$@" ;;
  backup)
    exec python -m lex_eval.eval_run --backup ;;
  dashboard)
    exec streamlit run lex_eval/reports/streamlit_report.py \
      --server.address 0.0.0.0 --server.port 8501 ;;
  help)
    cat <<'EOF'
Commands:
  check      test AILA (build and models) and OpenRouter, at no cost
  run        one evaluation run; "run --help" lists its options
  backup     copy the database, keeping the newest seven copies
  dashboard  the results dashboard on port 8501, reached through a tunnel
EOF
    ;;
  *)
    echo "Unknown command: $command (try: help)" >&2
    exit 1 ;;
esac
