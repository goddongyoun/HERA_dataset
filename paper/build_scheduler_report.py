"""Manifest-bound, canonical-validated scheduler tables and static figures."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from statistics import mean, median, stdev
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hera_v2.analysis import analyze, load_trial_summaries
from hera_v2.manifest import load_manifest
from hera_v2.scheduler import EmergencyContext, validate_emergency_semantics
from hera_v2.schema import EmergencyPreset

METHODS = ('hera_preempt', 'fifo_single', 'reserved_slot')
LABELS = ('Preempt', 'FIFO', 'Parallel slot')
COLORS = ('#166b8a', '#b45a43', '#688e3d')
DEADLINES = (1, 2, 5, 10, 30)


def support(leg):
    return tuple(x for name in ('FL', 'FR', 'BR', 'BL')
                 for x in ((0., 0., -.4) if name == leg else (0., -.08, -.48)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaign-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    campaign = args.campaign_dir.resolve()
    output = args.output_dir.resolve()
    if output.is_relative_to(campaign):
        parser.error('report output must be outside the immutable campaign directory')
    definition = json.loads((campaign/'campaign.json').read_text(encoding='utf-8'))
    groups = {}
    artifacts = []
    trial_artifacts = []
    audit_errors = []
    for entry in definition['manifests']:
        manifest = campaign/entry['snapshot_path']
        specs = load_manifest(manifest)
        if specs[0].study != 'scheduler':
            continue
        if hashlib.sha256(manifest.read_bytes()).hexdigest() != entry['snapshot_sha256']:
            raise ValueError('manifest hash changed')
        rows, _ = load_trial_summaries(specs, campaign/'trials', require_all=True)
        tokens = specs[0].mission_num_predict
        report = analyze(rows, [], specs=specs)
        saved_path = campaign/'analysis'/manifest.stem/'analysis.json'
        if saved_path.exists():
            saved_report = json.loads(saved_path.read_text(encoding='utf-8'))
            if any(saved_report.get(key) != value for key, value in report.items()):
                raise ValueError('saved scheduler analysis differs from recomputation: '+manifest.stem)
        group = {'tokens': tokens, 'rows': rows, 'report': report}
        groups[tokens] = group
        artifacts.append({'manifest': str(manifest), 'sha256': entry['snapshot_sha256']})
        for row in rows:
            raw = row['result']
            if raw['status'] == 'success':
                ctx = EmergencyContext('audit', (0,), (0.,), support(row['fault_leg']))
                try:
                    validate_emergency_semantics(EmergencyPreset.model_validate(raw['emergency_preset']), ctx)
                except Exception as exc:
                    audit_errors.append({'trial': row['trial_id'], 'error': str(exc)})
                if not raw.get('mission_active_at_fault', True):
                    audit_errors.append({'trial': row['trial_id'], 'error': 'successful contention trial without active mission'})
            events_path = campaign/'trials'/row['trial_id']/row['attempt_relative_dir']/'events.jsonl'
            events = [json.loads(line) for line in events_path.read_text(encoding='utf-8').splitlines() if line.strip()]
            trial_artifacts.append({'trial_id': row['trial_id'],
                                    'adopted_attempt_id': row['adopted_attempt_id'],
                                    'events_sha256': hashlib.sha256(events_path.read_bytes()).hexdigest(),
                                    'worker_summary_sha256': row['worker_summary_sha256']})
            accepted = [e for e in events if e['stage'] == 'response_accepted']
            if len(accepted) != int(raw['status'] == 'success'):
                audit_errors.append({'trial': row['trial_id'], 'error': 'acceptance event count mismatch'})
            if accepted:
                delta = (accepted[0]['monotonic_ns'] - raw['fault_monotonic_ns'])/1e6
                if abs(delta - raw['fault_to_accept_ms']) > 1e-8:
                    audit_errors.append({'trial': row['trial_id'], 'error': 'raw timestamp latency mismatch'})
    if audit_errors:
        raise ValueError(json.dumps(audit_errors))
    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10, 'axes.spines.top': False,
                         'axes.spines.right': False, 'svg.fonttype': 'none'})
    ordered = sorted(groups)
    fig, axes = plt.subplots(1, len(ordered), figsize=(12, 3.8), squeeze=False)
    numeric = {}
    lines = ['# Scheduler results', '',
             'All results are descriptive for one model, host and backend. The payload is a supplied canonical command, not discovered recovery.', '',
             'Latency summaries below use all manifested trials; any nonaccepted trial contributes its 30 s deadline, not a fictitious observed acceptance.', '',
             '| Mission token cap | Method | Accepted / N | Penalized mean (s) | Penalized SD (s) | Penalized median (s) |',
             '|---:|---|---:|---:|---:|---:|']
    for ax, tokens in zip(axes[0], ordered):
        group = groups[tokens]
        numeric[str(tokens)] = {}
        for index, (method, label, color) in enumerate(zip(METHODS, LABELS, COLORS)):
            rows = [r for r in group['rows'] if r['method'] == method]
            values = [float(r['event_time_s']) if r['success'] else float(r['deadline_s']) for r in rows]
            stats = group['report']['methods'][method]
            details = {**stats}
            details['retrospective_deadline_fraction'] = {str(d): sum(r['success'] and r['event_time_s']<=d for r in rows)/len(rows) for d in DEADLINES}
            for field in ('emergency_queue_ms', 'fault_to_dispatch_ms', 'fault_to_server_observed_ms', 'fault_to_first_chunk_ms'):
                field_values = [r['result'][field] for r in rows if r['result'].get(field) is not None]
                details[field+'_mean'] = mean(field_values) if field_values else None
            details['mission_status_counts'] = dict(Counter(req['status'] for r in rows for req in r['result'].get('requests', []) if req['request_type']=='mission'))
            for request_type in ('mission', 'emergency'):
                requests = [req for r in rows for req in r['result'].get('requests', [])
                            if req['request_type'] == request_type]
                for field in ('eval_count', 'prompt_eval_count'):
                    samples = [q['backend_metadata'][field] for q in requests
                               if q.get('backend_metadata', {}).get(field) is not None]
                    details[f'{request_type}_{field}'] = {
                        'n_reported': len(samples), 'n_requests': len(requests),
                        'mean': mean(samples) if samples else None,
                        'minimum': min(samples) if samples else None,
                        'maximum': max(samples) if samples else None}
            numeric[str(tokens)][method] = details
            positions = index + np.linspace(-.12, .12, len(values))
            ax.scatter(positions, values, s=17, color=color, alpha=.55, edgecolors='none')
            ax.plot([index-.2,index+.2], [mean(values)]*2, color='black', lw=2)
            lines.append(f"| {tokens} | {label} | {stats['n_success']}/{len(rows)} | {mean(values):.4f} | {stdev(values):.4f} | {median(values):.4f} |")
        ax.set_title(f'Mission cap: {tokens} tokens')
        ax.set_xticks(range(3), LABELS)
        ax.set_ylabel('Acceptance or deadline penalty (s)')
        ax.set_ylim(bottom=0)
        ax.grid(axis='y', alpha=.2)
    fig.suptitle('Canonical-command scheduling: all trials; nonacceptance = 30 s penalty', y=1.02)
    fig.tight_layout()
    for ext in ('png','svg'):
        fig.savefig(output/f'scheduler_latency.{ext}', dpi=300, bbox_inches='tight')
    plt.close(fig)
    fig, axes = plt.subplots(1, len(ordered), figsize=(12,3.6), squeeze=False)
    for ax,tokens in zip(axes[0],ordered):
        for method,label,color in zip(METHODS,LABELS,COLORS):
            fractions = numeric[str(tokens)][method]['retrospective_deadline_fraction']
            style = ({'marker': 's', 'markersize': 8, 'markerfacecolor': 'none',
                      'linestyle': '--', 'linewidth': 1.2, 'zorder': 4}
                     if method == 'reserved_slot' else
                     {'marker': 'o', 'markersize': 5, 'linewidth': 1.6, 'zorder': 3})
            ax.plot(DEADLINES,[fractions[str(d)] for d in DEADLINES],label=label,color=color,**style)
        ax.set_xscale('log')
        ax.set_xticks(DEADLINES,[str(d) for d in DEADLINES])
        ax.set_ylim(-.03,1.03)
        ax.set_title(f'Mission cap: {tokens}')
        ax.set_xlabel('Retrospective deadline (s)')
        ax.set_ylabel('Observed acceptance fraction')
        ax.grid(alpha=.2)
    fig.tight_layout()
    handles,labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles,labels,loc='lower center',bbox_to_anchor=(.5,-.06),ncol=3,frameon=False,fontsize=9)
    for ext in ('png','svg'):
        fig.savefig(output/f'scheduler_deadline_sensitivity.{ext}',dpi=300,bbox_inches='tight')
    plt.close(fig)
    lines += ['', '## Paired differences', '', '| Token cap | Contrast | Mean difference (s) | Batch-bootstrap 95% interval (s) |', '|---:|---|---:|---|']
    for tokens in ordered:
        for name, contrast in groups[tokens]['report']['paired_contrasts'].items():
            metric = contrast['metrics']['deadline_penalized_latency_s']
            low,high = metric['batch_cluster_bootstrap_95_ci']
            interval = f'[{low:.5f}, {high:.5f}]' if low is not None else 'N/A (fewer than two batches)'
            lines.append(f"| {tokens} | {name} | {metric['mean_difference']:.5f} | {interval} |")
    lines += ['', 'The deadline figure re-evaluates one set of observed 30-second-deadline trials; it is not a rerun at each timeout. No failed trial is dropped. Intervals are exploratory batch-cluster summaries, not confirmatory significance or deployment-general confidence.']
    (output/'scheduler_results.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    result = {'campaign': str(campaign), 'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'manifest_provenance': artifacts, 'canonical_and_raw_timing_audit_errors': audit_errors,
              'trial_artifacts': trial_artifacts,
              'paired_contrasts': {str(t): groups[t]['report']['paired_contrasts'] for t in ordered},
              'figure_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in output.iterdir() if p.suffix in ('.png', '.svg')},
              'n_trials': sum(len(g['rows']) for g in groups.values()), 'workloads': numeric}
    (output/'scheduler_report.json').write_text(json.dumps(result,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({'n_trials':result['n_trials'],'audit_errors':len(audit_errors),'output':str(output)}))


if __name__ == '__main__':
    main()
