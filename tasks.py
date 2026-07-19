# /// script
# requires-python = ">=3.11"
# dependencies = ["camas[mcp]>=0.1.27"]
# ///
"""Cerbos tasks — the single source of truth for local dev, CI, and agents.

camas replaces the task-runner / CI-orchestration role of the old ``.justfile``:
the serial ``lint`` chain becomes an inline ``Parallel``, and the hand-written
GitHub Actions test-split matrix (``strategy.matrix: split: [0..5]``) is emitted
from ``integration`` here via ``camas integration --github-matrix`` — the shard
count lives once, in this file.

``hack/tools/testsplit`` stays as a leaf: it balances Go packages across shards by
historical timing (a greedy bin-pack over ``test-times.json``), which is
orthogonal to the runner. camas owns the *fan-out*; ``testsplit`` owns the
intra-shard balancing.

Tool provisioning is NOT camas's job: it invokes ``go``, ``buf``,
``golangci-lint``, ``gotestsum``, ``actionlint``, ``modernize``, ``goreleaser``,
``govulncheck`` and ``testsplit`` from PATH. CI provisions them with
``cerbos/actions/install-tools`` plus ``hack/scripts/install-go-tools.sh``.
"""

from collections.abc import Sequence

from camas import Claude, Config, Parallel, Sequential, Task, run_cli

GO_ENV = {"CGO_ENABLED": "0", "GOAMD64": "v2"}
RACE_ENV = {"CGO_ENABLED": "1", "GOAMD64": "v2"}
MODERNIZE_ENV = GO_ENV | {"GOFLAGS": "-tags=tests,integration"}
PROTO = ("api", "buf.yaml", "buf.lock", "tools")
SPLITS = ("0", "1", "2", "3", "4", "5")


def _go_changed(changed: Sequence[str]) -> bool:
	return any(c.endswith(".go") for c in changed)


compile = Sequential(
	Task("go build ./...", name="go-build"),
	Task(
		("bash", "-c", "go test -tags=e2e,tests,integration -run=ignore ./... > /dev/null"),
		name="test-compile",
	),
	Task(
		"go build ./private/ruletable",
		name="wasm-build",
		env=GO_ENV | {"GOOS": "js", "GOARCH": "wasm"},
	),
	env=GO_ENV,
	when=_go_changed,
	help="compile all packages, the test binaries, and the wasm ruletable",
)

package = Task(
	"goreleaser release --config=.goreleaser.yml --snapshot --skip=announce,publish,validate,sign --clean",
	env=GO_ENV,
	when=_go_changed,
	help="goreleaser snapshot build",
)


lint_actions = Task("actionlint", name="lint-actions", when=".github")

go_lint = Parallel(
	Task("modernize -test ./...", name="modernize", env=MODERNIZE_ENV, when=_go_changed),
	Task(
		"golangci-lint run --config=.golangci.yaml",
		name="golangci-lint",
		env=GO_ENV,
		when=_go_changed,
	),
	help="Go linters (modernize + golangci-lint) in parallel",
)

buf = Parallel(
	Task("buf lint", name="buf-lint", when=PROTO),
	Task("buf format --diff --exit-code", name="buf-format", when=PROTO),
	help="proto lint + format check in parallel",
)

lint = Parallel(
	lint_actions,
	go_lint,
	buf,
	help="every linter in parallel (was the serial `just lint` chain)",
)

fix = Sequential(
	Task(
		"modernize -fix -test ./...",
		name="modernize-fix",
		env=MODERNIZE_ENV,
		mutates=True,
		when=_go_changed,
	),
	Task(
		"golangci-lint run --config=.golangci.yaml --fix",
		name="golangci-fix",
		env=GO_ENV,
		mutates=True,
		when=_go_changed,
	),
	Task("buf format -w", name="buf-format-fix", mutates=True, when=PROTO),
	help="auto-fix: modernize, golangci-lint --fix, buf format -w",
)


_TOTAL = len(SPLITS)
_SPLIT_CMD = (
	f"testsplit split --kind=integration --index={{SPLIT}} --total={_TOTAL} "
	f"--ignore-file=.ignore-packages.yaml | "
	f"xargs gotestsum --junitfile=junit.integration.{{SPLIT}}.xml "
	f"-- -tags=tests,integration -race -cover -covermode=atomic "
	f"-coverprofile=integration.{{SPLIT}}.cover"
)
integration = Parallel(
	Task(("bash", "-c", _SPLIT_CMD), name="integration-shard"),
	env=RACE_ENV,
	matrix={"SPLIT": SPLITS},
	when=_go_changed,
	help="race integration tests, time-balanced across shards by testsplit (axis: SPLIT)",
)

integration_times = Task(
	f"testsplit combine --kinds=integration --total={_TOTAL}",
	name="integration-times",
	help="rebuild test-times.json from the shard JUnit reports (feeds the next split)",
)

tests = Task(
	"gotestsum --format=dots-v2 --format-hide-empty-pkg -- -tags=tests,integration -failfast -count=1 ./...",
	env=GO_ENV,
	when=_go_changed,
	help="run the whole test suite via gotestsum (unsharded)",
)

vuln = Task(
	"govulncheck ./...",
	env=GO_ENV,
	when=_go_changed,
	help="govulncheck",
)

helm_lint = Task(
	"deploy/charts/validate.sh",
	name="helm-lint",
	when="deploy/charts",
	help="validate the Helm chart (helm lint + kubeconform + pluto)",
)


generate = Sequential(
	Task(
		("bash", "-c", "rm -rf api/genpb/cerbos internal/test/mocks schema/jsonschema schema/openapiv2"),
		name="gen-clean",
	),
	Task(
		(
			"bash",
			"-c",
			"set -euo pipefail\n"
			"buf format -w\n"
			"rm -rf api/genpb/cerbos\n"
			"( cd tools && buf generate --template=api.gen.yaml --output=.. )\n"
			"hack/scripts/remove-unused-protobuf-imports.sh\n"
			"GOWORK=off go mod tidy -C api/genpb",
		),
		name="gen-proto-code",
	),
	Task(
		(
			"bash",
			"-c",
			"set -euo pipefail\n"
			"rm -rf schema/jsonschema\n"
			"( cd tools && buf generate --template=jsonschema.gen.yaml --output=.. ../api/public )",
		),
		name="gen-json-schemas",
	),
	Task(
		(
			"bash",
			"-c",
			"set -euo pipefail\n"
			"schemas=internal/test/testdata/.jsonschema\n"
			'rm -rf "$schemas"\n'
			"( cd tools && buf generate --template=testdata_jsonschema.gen.yaml --output=.. ../api/private )\n"
			'mv "$schemas"/cerbos/private/v1/*TestCase.schema.json '
			'"$schemas"/cerbos/private/v1/QueryPlannerTestSuite.schema.json "$schemas"\n'
			'rm -rf "$schemas"/cerbos',
		),
		name="gen-testdata-schemas",
	),
	Task(
		("bash", "-c", 'set -euo pipefail\nrm -rf internal/test/mocks\nmockery --log-level=""'),
		name="gen-mocks",
	),
	Task("go run ./hack/tools/generate-npm-packages", name="gen-npm-packages"),
	Task(
		(
			"bash",
			"-c",
			"set -euo pipefail\n"
			"swagger=schema/openapiv2/cerbos/svc/v1/svc.swagger.json\n"
			"out=docs/modules/api/attachments\n"
			'docker run -e REDOCLY_TELEMETRY=off -v "$(pwd)":/cerbos redocly/cli:1.18.1 '
			'bundle "/cerbos/$swagger" -o "/cerbos/$out/cerbos-api" --ext json\n'
			'docker run -e REDOCLY_TELEMETRY=off -v "$(pwd)":/cerbos redocly/cli:1.18.1 '
			'build-docs "/cerbos/$swagger" -o "/cerbos/$out/cerbos-api.html"',
		),
		name="gen-api-docs",
	),
	Task(
		(
			"bash",
			"-c",
			"set -euo pipefail\n"
			"mkdir -p ./internal/confdocs\n"
			"go run -tags confdocs ./hack/tools/confdocs/confdocs.go > ./internal/confdocs/generated.go\n"
			"go run ./internal/confdocs/generated.go\n"
			"rm -rf ./internal/confdocs",
		),
		name="gen-confdocs",
	),
	Task("helm-schema -c deploy/charts/cerbos", name="gen-helm-schema"),
	env=GO_ENV,
	when=(*PROTO, "hack", "deploy/charts", "internal/test"),
	help="regenerate proto code, JSON schemas, mocks, confdocs and the helm values schema",
)


check = Parallel(compile, lint, help="fast static gate: compile + lint")

ci = Sequential(
	compile,
	Parallel(lint, integration, helm_lint, vuln, name="ci-checks"),
	help="reproduce the CI quality gates locally (compile, then everything in parallel)",
)

_ = Config(default_task=check, github_task=check, agent=Claude(fix=fix, check=check))

if __name__ == "__main__":
	run_cli(globals())
