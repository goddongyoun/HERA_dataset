"""Re-run each frozen analyzer and compare every saved numerical analysis field.

This is a computational reproducibility check, not an independent endpoint
derivation. build_physics_report.py provides the separate raw-trace derivation.
Outputs are always outside the immutable campaign.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    campaign, output = args.campaign_dir.resolve(), args.output_dir.resolve()
    if output.is_relative_to(campaign):
        parser.error('output must be outside the immutable campaign')
    output.mkdir(parents=True, exist_ok=True)
    definition = json.loads((campaign / 'campaign.json').read_text(encoding='utf-8'))
    provenance = json.loads((campaign / 'provenance.json').read_text(encoding='utf-8'))
    snapshot = campaign / 'source_snapshot'
    for entry in provenance['source_files']:
        if digest(snapshot / entry['file']) != entry['sha256']:
            raise ValueError('frozen source hash mismatch: ' + entry['file'])
    records = []
    for entry in definition['manifests']:
        manifest = campaign / entry['snapshot_path']
        if digest(manifest) != entry['snapshot_sha256']:
            raise ValueError('manifest hash mismatch: ' + str(manifest))
        folder = output / manifest.stem
        folder.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, '-B', str(snapshot / 'analyze_results.py'),
                   str(manifest), '--results-dir', str(campaign / 'trials'),
                   '--require-all', '--output-dir', str(folder)]
        completed = subprocess.run(command, cwd=snapshot, capture_output=True,
                                   text=True, encoding='utf-8', timeout=300)
        (folder / 'stdout.log').write_text(completed.stdout, encoding='utf-8')
        (folder / 'stderr.log').write_text(completed.stderr, encoding='utf-8')
        saved = campaign / 'analysis' / manifest.stem / 'analysis.json'
        recalculated = folder / 'analysis.json'
        exact = (completed.returncode == 0 and recalculated.is_file()
                 and json.loads(saved.read_text(encoding='utf-8'))
                 == json.loads(recalculated.read_text(encoding='utf-8')))
        records.append({'manifest': entry['snapshot_path'],
                        'manifest_sha256': digest(manifest),
                        'returncode': completed.returncode, 'exact_json_match': exact,
                        'saved_analysis_sha256': digest(saved),
                        'recalculated_analysis_sha256': digest(recalculated) if recalculated.is_file() else None,
                        'command': command})
    result = {'created_utc': datetime.now(timezone.utc).isoformat(),
              'campaign': str(campaign), 'source_sha256': provenance['source_sha256'],
              'script_sha256': digest(Path(__file__)),
              'all_match': all(r['exact_json_match'] for r in records),
              'manifests': records,
              'scope': 'Exact recomputation using archived source, distinct from independent physical endpoint audit.'}
    (output / 'verification.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'all_match': result['all_match'], 'manifests': len(records),
                      'output': str(output / 'verification.json')}))
    return 0 if result['all_match'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
