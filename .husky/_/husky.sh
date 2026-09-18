#!/usr/bin/env sh
if [ -z "$husky_skip_init" ]; then
  debug () {
    [ "$HUSKY_DEBUG" = "1" ] && echo "husky (debug) - $1"
  }

  readonly hook_name="$(basename -- "$0")"
  readonly husky_dir="$(dirname -- "$0")/.."

  if [ -f "$husky_dir/.huskyrc" ]; then
    debug "source $husky_dir/.huskyrc"
    . "$husky_dir/.huskyrc"
  fi

  readonly husky_skip_init=1
  export husky_skip_init
  sh -v "$husky_dir/$hook_name" "${1:-}"
fi
