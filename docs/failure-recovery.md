# Failure recovery

## One bad file

The top-level batch loop catches file-level exceptions, records traceback and
continues. After all files are attempted, the batch exits nonzero if failures
remain. Re-run after correcting only the failed input; valid completed outputs
are reused through their integrity state.

## MinerU timeout or engine death

MinerU runs in a dedicated POSIX process group. Total timeout, idle timeout,
controller interruption and lingering children all enter process-group cleanup:
SIGTERM, bounded grace period, then SIGKILL if necessary.

Errors such as temporary service unavailability, connection reset, NCCL failure,
`EngineCore failed` and resource exhaustion are classified as transient. Retry
the same task first. Recursive splitting is for persistent content/task failure,
not the first infrastructure failure.

## Checkpoints

Checkpoint JSON carries `_checkpoint_sha256`. Missing, malformed or mismatched
checksums invalidate reuse. Completed-state migration first authenticates the old
identity hash, then applies timestamp-free model-cache normalization.

Never edit checkpoint files manually. Remove only the narrowly identified job
checkpoint when deliberately forcing recomputation; retain its log for diagnosis.

## Deployment rollback

Changed deployments create a timestamped `.deploy-backups` copy under the
explicit deployment target. Restore only the exact intended file and
verify SHA-256 before running. Backups are runtime artifacts and must not be
committed.
