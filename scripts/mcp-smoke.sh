#!/usr/bin/env bash
set -euo pipefail

: "${MCP_URL:?MCP_URL is required}"
: "${MCP_ACCESS_TOKEN:?MCP_ACCESS_TOKEN is required}"

smoke_dir="$(mktemp -d)"
trap 'rm -rf "$smoke_dir"' EXIT
request_id=0
latencies=()

rpc() {
  local label="$1"
  local method="$2"
  local params="$3"
  request_id=$((request_id + 1))
  local output="$smoke_dir/$label.json"
  local timing
  timing="$(curl -fsS -o "$output" -w '%{time_total}' \
    -H "Authorization: Bearer $MCP_ACCESS_TOKEN" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -X POST "$MCP_URL" \
    --data "{\"jsonrpc\":\"2.0\",\"id\":$request_id,\"method\":\"$method\",\"params\":$params}")"
  jq -e '.error == null' "$output" >/dev/null
  latencies+=("$timing")
  printf '%s latency=%ss\n' "$label" "$timing"
}

tool() {
  local label="$1"
  local name="$2"
  local arguments="$3"
  rpc "$label" tools/call "{\"name\":\"$name\",\"arguments\":$arguments}"
  if ! jq -e '.result.isError == false' "$smoke_dir/$label.json" >/dev/null; then
    jq -c '{isError: .result.isError, error: .result.structuredContent.error,
      message: .result.structuredContent.message}' "$smoke_dir/$label.json"
    return 1
  fi
}

rpc initialize initialize '{}'
rpc tools tools/list '{}'
jq -e '.result.tools | length == 10' "$smoke_dir/tools.json" >/dev/null

tool operations get_operations_summary '{}'
tool needs_attention get_needs_attention '{"limit":5}'
tool signals search_signals '{"vertical":"CHILDRENS_HOME","limit":1}'
signal_id="$(jq -r '.result.structuredContent.items[0].signal_id // empty' "$smoke_dir/signals.json")"
if [[ -n "$signal_id" ]]; then
  tool signal get_signal "{\"signal_id\":\"$signal_id\"}"
fi

tool opportunities search_opportunities '{"vertical":"CHILDRENS_HOME","q":"Ashburton","limit":1}'
opportunity_id="$(jq -r '.result.structuredContent.items[0].opportunity_id // empty' "$smoke_dir/opportunities.json")"
if [[ -z "$opportunity_id" ]]; then
  tool opportunity_fallback search_opportunities '{"vertical":"CHILDRENS_HOME","limit":1}'
  opportunity_id="$(jq -r '.result.structuredContent.items[0].opportunity_id // empty' "$smoke_dir/opportunity_fallback.json")"
fi
test -n "$opportunity_id"
tool opportunity get_opportunity "{\"opportunity_id\":\"$opportunity_id\"}"

tool manual_review search_opportunities '{"vertical":"CHILDRENS_HOME","publication_policy_outcome":"MANUAL_REVIEW","limit":1}'
manual_id="$(jq -r '.result.structuredContent.items[0].opportunity_id // empty' "$smoke_dir/manual_review.json")"
if [[ -n "$manual_id" ]]; then
  tool manual_explanation get_opportunity "{\"opportunity_id\":\"$manual_id\"}"
  jq -e '.result.structuredContent.opportunity.publication.policy_outcome == "MANUAL_REVIEW"' \
    "$smoke_dir/manual_explanation.json" >/dev/null
fi

tool publication_qa get_review_backlog '{"queue":"publication_qa","limit":5}'
tool unmatched get_review_backlog '{"queue":"unmatched_strong","vertical":"CHILDRENS_HOME","limit":5}'
tool sources get_source_status '{"source":"all"}'
tool automation get_automation_status '{}'
tool recent get_recent_changes '{"last_hours":24,"limit":20}'

printf 'tools=%s needs_attention=%s qa_holdouts=%s unmatched_strong=%s recent_changes=%s\n' \
  "$(jq -r '.result.tools | length // 0' "$smoke_dir/tools.json")" \
  "$(jq -r '.result.structuredContent.total // 0' "$smoke_dir/needs_attention.json")" \
  "$(jq -r '.result.structuredContent.total // 0' "$smoke_dir/publication_qa.json")" \
  "$(jq -r '.result.structuredContent.total // 0' "$smoke_dir/unmatched.json")" \
  "$(jq -r '.result.structuredContent.count // 0' "$smoke_dir/recent.json")"

printf '%s\n' "${latencies[@]}" | awk \
  '{sum += $1; if ($1 > max) max = $1; n += 1} END {printf "latency_average=%.3fs latency_max=%.3fs calls=%d\n", sum/n, max, n}'

jq -c '.result.structuredContent | {
  watcher: {
    enabled: .watcher.enabled,
    policy_version: .watcher.policy_version,
    enabled_watches: .watcher.enabled_watches,
    provider_requests_today: .watcher.provider_requests_today,
    provider_requests_current_month: .watcher.provider_requests_current_month,
    failed_polls: .watcher.polls.failed
  },
  publication: {
    enabled: .publication.enabled,
    policy_version: .publication.policy_version,
    total_published: .publication.total_published,
    automatically_published: .publication.automatically_published,
    latest_failed: .publication.latest_execution.failed
  },
  withdrawal: {
    enabled: .withdrawal.enabled,
    policy_version: .withdrawal.policy_version,
    total_automatic_withdrawals: .withdrawal.total_automatic_withdrawals,
    latest_failed: .withdrawal.latest_execution.failed
  }
}' "$smoke_dir/automation.json"
