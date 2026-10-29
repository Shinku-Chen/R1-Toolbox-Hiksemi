#!/bin/sh
set -eu

case "${1:-run}" in
  --help|-h|help)
    cat <<'HELP'
R1 Toolbox

Usage: r1-toolbox [run|--help|--version]

  run         Keep the NAS Web/control runtime running until the container stops.
  --help      Show this help without accessing the NAS host.
  --version   Show the image version without accessing the NAS host.

Deploy with composer.yml for the required host mounts and namespaces.
composer.yml uses UI_MODE=image: the bundled UI runs from the host's /run tmpfs.
The Web interface controls the small-screen takeover independently of MCP.
UI_MODE=diagnostic leaves the display alone; UI_MODE=directory uses NAS UI_DIR.
Without UI_MODE, the bundled screen is available and initially enabled.
HELP
    ;;
  --version|-V|version)
    printf 'R1 Toolbox %s\n' "${TOOLBOX_VERSION:-dev}"
    ;;
  run)
    if [ "$#" -gt 1 ]; then
      echo '[ERROR] run 不接受额外参数；请使用 --help 查看用法。' >&2
      exit 2
    fi
    TASK_CONTAINER=${TASK_CONTAINER:-r1-toolbox}
    export TASK_CONTAINER
    exec /bin/sh /opt/r1-toolbox/runner.sh
    ;;
  *)
    printf '[ERROR] 未知命令：%s；请使用 --help 查看用法。\n' "$1" >&2
    exit 2
    ;;
esac
