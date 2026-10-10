#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
CONTEXT_DIR="${REPO_ROOT}/.open-design-context"
OPEN_DESIGN_DIR="${REPO_ROOT}/open-design"
OUTPUT_BASE="${REPO_ROOT}/polymarket_bot_v2/scripts/frontend/.design-pipeline"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/ui_design_pipeline.sh check
  bash scripts/ui_design_pipeline.sh brief --area <name> --goal <text> [--surface <dashboard|admin|landing>] [--mode <refactor|new>]
  bash scripts/ui_design_pipeline.sh start-od [--port <n>] [--host <addr>] [--no-open]

Commands:
  check     Validate Open Design and context prerequisites.
  brief     Generate OD + v0 prompt pack with default context constraints.
  start-od  Launch Open Design daemon from local open-design clone.
EOF
}

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo "Missing required file: $path" >&2
    exit 1
  fi
}

run_check() {
  require_file "${CONTEXT_DIR}/DESIGN.md"
  require_file "${CONTEXT_DIR}/tokens.json"
  require_file "${CONTEXT_DIR}/layout.rules.json"
  require_file "${OPEN_DESIGN_DIR}/apps/daemon/dist/cli.js"
  require_file "${REPO_ROOT}/polymarket_bot_v2/scripts/frontend/templates/skeleton.html"
  require_file "${REPO_ROOT}/polymarket_bot_v2/scripts/static/Vgril_left.png"
  require_file "${REPO_ROOT}/polymarket_bot_v2/scripts/static/Veyra.png"

  echo "Context directory: ${CONTEXT_DIR}"
  echo "Open Design CLI: ${OPEN_DESIGN_DIR}/apps/daemon/dist/cli.js"
  echo "Check result: OK"
}

context_hash() {
  sha256sum \
    "${CONTEXT_DIR}/DESIGN.md" \
    "${CONTEXT_DIR}/tokens.json" \
    "${CONTEXT_DIR}/layout.rules.json" \
    | sha256sum \
    | awk '{print $1}'
}

run_brief() {
  local area=""
  local goal=""
  local surface="dashboard"
  local mode="refactor"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --area)
        area="${2:-}"
        shift 2
        ;;
      --goal)
        goal="${2:-}"
        shift 2
        ;;
      --surface)
        surface="${2:-}"
        shift 2
        ;;
      --mode)
        mode="${2:-}"
        shift 2
        ;;
      *)
        echo "Unknown argument: $1" >&2
        exit 1
        ;;
    esac
  done

  if [[ -z "$area" || -z "$goal" ]]; then
    echo "brief requires --area and --goal" >&2
    exit 1
  fi

  run_check >/dev/null
  mkdir -p "$OUTPUT_BASE"

  local stamp run_dir hash
  stamp="$(date +%Y%m%d_%H%M%S)"
  run_dir="${OUTPUT_BASE}/${stamp}_${area}"
  hash="$(context_hash)"

  mkdir -p "${run_dir}/context"
  cp "${CONTEXT_DIR}/DESIGN.md" "${run_dir}/context/DESIGN.md"
  cp "${CONTEXT_DIR}/tokens.json" "${run_dir}/context/tokens.json"
  cp "${CONTEXT_DIR}/layout.rules.json" "${run_dir}/context/layout.rules.json"

  cat >"${run_dir}/open-design.prompt.md" <<EOF
# Open Design Prompt Pack

## Task
- Area: ${area}
- Surface: ${surface}
- Mode: ${mode}
- Goal: ${goal}

## Hard Requirements
- Keep V2 architecture boundaries; no V1 fallback paths.
- Reuse real APIs and existing data flows; no fake UI actions.
- Keep i18n key-based copy updates; avoid hardcoded Chinese fallback.
- Ensure hover/focus/active/disabled states are complete.
- Ensure responsive behavior for 320, 768, and 1280 breakpoints.
- Enforce triad baseline as mandatory:
  - skeleton baseline from polymarket_bot_v2/scripts/frontend/templates/skeleton.html
  - brand character anchor from polymarket_bot_v2/scripts/static/Vgril_left.png
  - wordmark/logo anchor from polymarket_bot_v2/scripts/static/Veyra.png
- Preserve skeleton component anchors (v-page-shell, v-page-hero, v-card, v-btn, badge).
- Do not introduce a new global visual language outside existing V2 tokens.

## Design Context (Default)
Apply the attached context files as mandatory constraints:
- ./context/DESIGN.md
- ./context/tokens.json
- ./context/layout.rules.json

Context hash: ${hash}

## Output Contract
1. Provide component/page structure and interaction flow.
2. Provide token mapping table (new tokens vs existing V2 tokens).
3. Provide implementation plan referencing real files in polymarket_bot_v2.
4. List preserved skeleton anchors and any required brand-asset placement.
5. Provide risk list and verification checklist.
EOF

  cat >"${run_dir}/v0.prompt.md" <<EOF
# v0 Prompt Pack (Aligned With Open Design)

Design a ${surface} UI for ${area}.

Primary goal:
${goal}

Mandatory constraints:
- Use attached design context as source of truth.
- Enforce triad baseline as mandatory:
  - skeleton baseline from polymarket_bot_v2/scripts/frontend/templates/skeleton.html
  - brand character anchor from polymarket_bot_v2/scripts/static/Vgril_left.png
  - wordmark/logo anchor from polymarket_bot_v2/scripts/static/Veyra.png
- Respect existing dark base + blue action hierarchy from tokens.
- Keep gold highlights optional and small; never use gold as primary action fill.
- Keep thin borders, readable contrast, and clear hierarchy.
- Include full states: hover/focus/active/disabled.
- Produce desktop + mobile layouts (320/768/1280 breakpoints).
- Output should be implementation-ready for existing V2 code, not a standalone demo.
- Preserve skeleton component anchors (v-page-shell, v-page-hero, v-card, v-btn, badge).

Attached context:
- ./context/DESIGN.md
- ./context/tokens.json
- ./context/layout.rules.json

Expected output:
- Page-level layout
- Component list
- Token usage notes
- Interaction notes
- Accessibility checks
EOF

  cat >"${run_dir}/handoff.json" <<EOF
{
  "createdAt": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "area": "${area}",
  "surface": "${surface}",
  "mode": "${mode}",
  "goal": "${goal}",
  "contextHash": "${hash}",
  "contextDir": "${CONTEXT_DIR}",
  "openDesignPrompt": "${run_dir}/open-design.prompt.md",
  "v0Prompt": "${run_dir}/v0.prompt.md"
}
EOF

  echo "Prompt pack generated: ${run_dir}"
  echo "- ${run_dir}/open-design.prompt.md"
  echo "- ${run_dir}/v0.prompt.md"
  echo "- ${run_dir}/handoff.json"
}

run_start_od() {
  if [[ ! -d "$OPEN_DESIGN_DIR" ]]; then
    echo "Missing open-design repository: ${OPEN_DESIGN_DIR}" >&2
    exit 1
  fi

  cd "$OPEN_DESIGN_DIR"
  node apps/daemon/dist/cli.js "$@"
}

main() {
  local command="${1:-}"
  if [[ -z "$command" ]]; then
    usage
    exit 1
  fi
  shift || true

  case "$command" in
    check)
      run_check
      ;;
    brief)
      run_brief "$@"
      ;;
    start-od)
      run_start_od "$@"
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      echo "Unknown command: ${command}" >&2
      usage
      exit 1
      ;;
  esac
}

main "$@"