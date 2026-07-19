#!/usr/bin/env bash
#
# Copyright 2021-2026 Zenauth Ltd.
# SPDX-License-Identifier: Apache-2.0
#
# Install the Go tools that camas (tasks.py) invokes, pinned to tools/go.mod.
# Usage: install-go-tools.sh [tool ...]  (default: all)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${GOBIN:=$(go env GOPATH)/bin}"
export GOBIN
mkdir -p "$GOBIN"

declare -A pkg=(
	[modernize]="golang.org/x/tools/go/analysis/passes/modernize/cmd/modernize"
	[gotestsum]="gotest.tools/gotestsum"
	[govulncheck]="golang.org/x/vuln/cmd/govulncheck"
	[mockery]="github.com/vektra/mockery/v3"
	[helm-schema]="github.com/dadav/helm-schema/cmd/helm-schema"
)
declare -A module=(
	[modernize]="golang.org/x/tools"
	[gotestsum]="gotest.tools/gotestsum"
	[govulncheck]="golang.org/x/vuln"
	[mockery]="github.com/vektra/mockery/v3"
	[helm-schema]="github.com/dadav/helm-schema"
)

want=("$@")
[[ ${#want[@]} -eq 0 ]] && want=(modernize gotestsum govulncheck mockery helm-schema testsplit)

install_go_tool() {
	local name="$1" version
	if [[ "$name" == "testsplit" ]]; then
		echo "build testsplit"
		GOWORK=off go install -C "$ROOT/hack/tools/testsplit"
		return
	fi
	version="$(cd "$ROOT/tools" && GOWORK=off go list -m -f '{{.Version}}' "${module[$name]}")"
	echo "install ${name}@${version}"
	GOWORK=off go install "${pkg[$name]}@${version}"
}

for name in "${want[@]}"; do
	install_go_tool "$name"
done

echo "provisioned into ${GOBIN}:"
for name in "${want[@]}"; do
	printf '  %-14s %s\n' "$name" "$(command -v "$name" 2>/dev/null || echo "MISSING (is ${GOBIN} on PATH?)")"
done
