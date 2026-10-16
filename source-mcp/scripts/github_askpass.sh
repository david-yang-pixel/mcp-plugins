#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' "x-access-token" ;;
  *Password*) printf '%s\n' "$SOURCE_MCP_GITHUB_TOKEN" ;;
  *) printf '\n' ;;
esac
