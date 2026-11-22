# Security Scan Report - 2026-11-22

## Scope

Repository scanned on the Linux server at `/root/backup-platform-private`, branch `main`.
The scan covered tracked Python source, dependency declarations, credential patterns, shell execution sites, archive/path handling, and repository state.

## Results

### No critical secret exposure found

Tracked files contain no detected GitHub tokens, AWS access keys, or PEM private key material. Runtime credentials are expected to be supplied through configuration or protected task records.

### Manual review findings

1. `core/os_backup.py` uses `subprocess.run(..., shell=True)` for a fixed internal metadata command list. The commands are assembled from platform-owned constants and are not directly built from request input. Keep the command list closed and avoid adding user-controlled fragments.
2. Other `shell=True` and subprocess sites exist in database tooling, plugin installation, and platform probing. They require call-chain review whenever new user-controlled arguments are introduced. Existing code should continue to use argument arrays or strict quoting for external values.
3. Dependency vulnerability audit was not completed because the machine rollback left the available Python 3.11 runtime without its standard library. `bandit` and `pip-audit` could not start in that environment. Re-run both after restoring a complete Python runtime and dependency environment.
4. `requirements.txt` uses minimum version constraints for most dependencies. Reproducible deployment should use a reviewed lock file or hashed wheel set before production rollout.

## Decision

No credential leak or immediately exploitable high-severity finding was detected by the available scans. The repository is pushed with this report. Dependency and Bandit scans remain explicitly pending because the rolled-back server runtime is incomplete.

## Recommended follow-up

Restore the server Python runtime, run `bandit -r api core app.py run.py`, run `pip-audit -r requirements.txt`, review every high or medium finding, then update the lock file and repeat the scan.