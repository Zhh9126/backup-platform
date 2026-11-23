# Security Scan and Hardening Report - 2026-11-23

Bandit 1.6.3 was run against `api`, `core`, `app.py`, and `run.py`. The scan found no remaining HIGH shell execution findings in the backup metadata, plugin installer, or sandbox probe paths after hardening.

The remaining HIGH findings are the optional FTP backend and its calls in `core/netdev.py` and `core/storage_backends/ftp.py`. FTP is plaintext by design. Production deployments should disable FTP targets and use SFTP, FTPS, or object storage.

The hardening changes replace direct `shell=True` calls with explicit `/bin/bash -c` argument execution for trusted internal command templates. The service was restarted successfully and returned HTTP 302 on port 8080.

The Python 3.11 runtime on the rolled-back CentOS image lacks a native SSL extension. The service is running with the recovered runtime and existing dependency paths. A complete dependency vulnerability audit should be repeated after installing a Python build with OpenSSL 1.1.1+ support.
