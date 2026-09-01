# Adapter — test runners

How to get per-test timings and turn on parallelism, per runner. For the decision framework — what the timings mean and how to check parallel-safety — see Steps 3 and 4 of `SKILL.md`.

## Getting per-test timings

| Runner | Emit timings | Parse |
|---|---|---|
| MSTest / xUnit / NUnit (.NET) | `dotnet test --logger "trx;LogFileName=t.trx"` | TRX is XML; `UnitTestResult/@duration` is `hh:mm:ss.fffffff`, join to `UnitTest/TestMethod/@className` via `@testId` |
| pytest | `pytest --durations=0` | text, or `pytest --json-report --json-report-file=r.json` (pytest-json-report). `--json-report-file` only sets the path — without `--json-report` nothing is written |
| Jest / Vitest | `--json --outputFile=r.json` | `testResults[].assertionResults[].duration` (ms) |
| Go | `go test -json ./...` | `Action == "pass"` records **that also carry a `Test` field**. Records without `Test` are package-level totals; counting both double-counts every test |
| Maven (surefire) | JUnit XML in `target/surefire-reports/` | `<testcase time="...">` |
| Gradle | JUnit XML in `build/test-results/<testTaskName>/` (e.g. `build/test-results/test/`) | `<testcase time="...">` |
| RSpec | `--profile` or `--format json` | `examples[].run_time` |
| Playwright | `--reporter=json` | `suites[].specs[].tests[].results[].duration` |

Always compute **both**: per-class/per-file totals (finds the hot fixture) and the slowest individual tests (finds deliberate sleeps). They point at different fixes.

Compare the **sum of test durations to the step's wall-clock**. A large gap is build, restore, or fixture cost living outside the tests, and no amount of test parallelism touches it.

## Turning on parallelism

**MSTest** — serial by default. In an `AssemblyInfo.cs`:

```csharp
[assembly: Parallelize(Workers = 8, Scope = ExecutionScope.ClassLevel)]
```

`Workers = 0` means one per processor. `ClassLevel` runs classes concurrently but keeps methods within a class sequential — which is what you want when classes hold their fixture in `static` fields set up in `[ClassInitialize]`. `MethodLevel` requires no shared state at all within a class.

**xUnit** — parallel across collections by default; tests in one collection are serial. `maxParallelThreads` in `xunit.runner.json`. Check for a shared `[Collection]` forcing serialisation.

**NUnit** — `[assembly: Parallelizable(ParallelScope.Fixtures)]` + `[assembly: LevelOfParallelism(8)]`.

**pytest** — `pytest -n 8` (pytest-xdist). `--dist loadfile` keeps a file's tests on one worker, the usual choice with file-scoped fixtures.

**Jest / Vitest** — parallel by default; `--maxWorkers=N`. Check for `--runInBand` or `maxWorkers: 1` left behind from debugging a flake, which is a common accidental serialisation.

**Go** — packages already run in parallel; `-p N`. Within a package, tests need an explicit `t.Parallel()`.

**Maven surefire** — `<parallel>classes</parallel>` + `<threadCount>8</threadCount>`.

**Gradle** — a different mechanism, not the surefire settings: `Test.maxParallelForks` (default `1`) forks N test JVMs and distributes classes across them.

```kotlin
tasks.test { maxParallelForks = 8 }
```

Each fork is a whole JVM, so this costs more memory per worker than an in-process thread pool — cap it accordingly.

## Choosing the worker count

From Step 3's classification:

- **CPU-bound** — the core count. More just adds context switching.
- **Blocked** (app boots, sleeps, retry backoff, network, containers) — **above** the core count. A blocked worker holds no core. This is the case people get wrong, because "one per processor" is the standard advice and it leaves a blocked suite starved.
- **Memory-bound** — each worker holding a booted application plus its own database costs real RAM. Cap it, and say in a comment that raising it needs a memory check as well as a timing one.

Measure on the CI runner, not the dev machine. Dev boxes have far more cores; a setting that looks marginally worse locally can be clearly better in CI, and a local suite already at its floor cannot show you a CI gain at all.

**Know the floor.** If methods within a unit stay sequential, total time can never go below the slowest single unit. When measured parallel time ≈ the slowest class's total, you are at the floor and more workers will do nothing — the only way lower is splitting that class.

## What makes a suite slow, in the order worth checking

1. **Per-unit fixture setup that boots an application.** N classes × a full app boot. Parallelism hides it; sharing a fixture across read-only classes removes it, at the cost of isolation.
2. **Deliberate sleeps.** Tests exercising timeouts, retry policies or backoff genuinely wait. **Do not shorten the backoff to make CI faster** — that is Step 5's "never reduce coverage" line, because the wait is the thing under test. Overlap it with parallelism instead.
3. **A serial-by-default runner.** Free win once Step 4 clears it.
4. **Real I/O that could be in-memory** — a containerised database where an in-memory one would exercise the same code. Only if it does not weaken the test; a suite that exists to catch provider-specific behaviour must keep the real provider.
5. **Rebuilding between steps.** `--no-build` / `--no-restore` on later steps that follow a build in the same job.

## Proving it is safe

A parallelism change is a correctness change. Run the suite **several times consecutively** and require every run green — four consecutive is a reasonable bar for ~1,000 tests. Record the durations too; a suite whose time varies wildly run to run is contending on something and is not actually safe yet.

If it flakes, do not lower the worker count and call it fixed. That hides the race rather than removing it, and it will resurface on a differently-sized runner. Find the shared resource.
