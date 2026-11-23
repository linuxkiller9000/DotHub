#!/usr/bin/env bash
# Guided local helper: prints reviewed commands, backs up paths, and never runs imported scripts.
set -euo pipefail
say(){ printf '%s\n' "$*"; }
case "${1:-help}" in
  detect)
    if [[ -r /etc/os-release ]]; then . /etc/os-release; say "Distribution: ${PRETTY_NAME:-unknown}"; fi
    for manager in pacman apt dnf zypper flatpak snap; do command -v "$manager" >/dev/null 2>&1 && say "Available: $manager"; done
    exit 0
    ;;
  plan)
    if [[ $# -lt 2 ]]; then say 'Usage: installer.sh plan RESOURCE_ARCHIVE [DESTINATION]'; exit 2; fi
    archive=$2; dest=${3:-"$HOME/.config/dothub-preview"}
    [[ -f "$archive" ]] || { say 'Archive does not exist.'; exit 2; }
    command -v unzip >/dev/null 2>&1 || { say 'Install unzip using your distribution package manager, then retry.'; exit 2; }
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    unzip -Z1 -- "$archive" | while IFS= read -r entry; do
      [[ "$entry" != /* && "$entry" != *'..'* ]] || { say "Unsafe archive path: $entry"; exit 3; }
      say "Would copy: $entry → $dest/$entry"
    done
    say 'No files were changed. Review the list, then install manually after backing up your config.'
    ;;
  help|*) say 'Usage: installer.sh detect | plan RESOURCE_ARCHIVE [DESTINATION]'; say 'DotHub does not install packages or execute imported scripts.' ;;
esac
