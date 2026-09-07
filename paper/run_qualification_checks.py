"""Persist unit/regression qualification with exact execution-source identity."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hera_v2.provenance import source_digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_before = source_digest(root)
    command = [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v']
    result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                            encoding='utf-8', timeout=120)
    source_after = source_digest(root)
    log = result.stdout + result.stderr
    (output/'unit_tests.log').write_text(log, encoding='utf-8')
    count = re.search(r'Ran (\d+) tests', log)
    success = result.returncode == 0 and source_before == source_after and count is not None
    report = {'created_utc': datetime.now(timezone.utc).isoformat(), 'command': command,
              'source_sha256': source_before, 'source_unchanged': source_before == source_after,
              'returncode': result.returncode, 'tests_run': int(count[1]) if count else None,
              'passed': success, 'log_sha256': hashlib.sha256(log.encode('utf-8')).hexdigest(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (output/'qualification.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report))
    return 0 if success else 2


if __name__ == '__main__':
    raise SystemExit(main())
