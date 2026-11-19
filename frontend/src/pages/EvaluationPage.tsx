import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { EvidenceHistory } from '../components/EvidenceHistory';
import { EvidenceLog } from '../components/EvidenceLog';
import { PageHeader } from '../components/PageHeader';
import { StatusBadge } from '../components/StatusBadge';
import { useProject } from '../context/ProjectContext';
import { workflow } from '../services/workflowService';
import type { BenchmarkMetrics, EvaluationReport, Trial } from '../types/evaluation';

const displayRate = (rate: BenchmarkMetrics['verified_success']) =>
  rate.value === null ? 'Unavailable' : (100 * rate.value).toFixed(1) + '% (' + rate.numerator + '/' + rate.denominator + ')';

function TrialEvidence({ trial, id }: { trial: Trial; id: string }) {
  return <details className="history-row">
    <summary>{trial.role} · {trial.label} · Run {trial.attempt} · {trial.reward === null ? 'No reward' : 'Reward ' + trial.reward}</summary>
    <div className="workflow-panel">
      <p>{trial.origin === 'test_double' ? 'Test fixture evidence' : 'Harbor execution evidence'} · {trial.trustworthy ? 'Trustworthy' : 'Unscored evidence'}</p>
      {trial.configuration && <p>Model {trial.configuration.model}</p>}
      {trial.requested_model && <p>Fallback from {trial.requested_model}</p>}
      {trial.failure && <p>Failure category {trial.failure.replace(/_/g, ' ')}</p>}
      {trial.agent_name && <p>{trial.agent_name} {trial.agent_version} · {trial.resolved_models.join(', ')}</p>}
      {trial.flags.length > 0 && <ul>{trial.flags.map(flag => <li key={flag}>{flag}</li>)}</ul>}
      {trial.process && <>
        <p>{trial.process.timed_out ? 'Timed out' : trial.process.exit_code === null ? 'No exit code' : 'Exit ' + trial.process.exit_code}</p>
        <pre className="evidence-command">{trial.process.argv.join(' ')}</pre>
        {trial.process.console_log && <EvidenceLog path={'/history/evaluation/' + id + '/logs/' + encodeURIComponent(trial.process.console_log)} />}
      </>}
      {trial.submitted_files !== null && <div>
        <strong>{trial.submitted_files.length} changed {trial.submitted_files.length === 1 ? 'file' : 'files'}</strong>
        <ul>{trial.submitted_files.map(file => <li key={file.path}>{file.operation} <code>{file.path}</code></li>)}</ul>
      </div>}
    </div>
  </details>;
}

export function EvaluationPage() {
  const { task, solution, testSuite, validationReport, active, queue, job } = useProject();
  const [mode, setMode] = useState<'control' | 'agent'>('control');
  const [model, setModel] = useState('');
  const [comparison, setComparison] = useState('');
  const [fallback, setFallback] = useState('');
  const [repeats, setRepeats] = useState(1);
  const [selected, setSelected] = useState<{ taskId: string; id: string } | null>(null);
  const [report, setReport] = useState<EvaluationReport | null>(null);
  const [error, setError] = useState('');
  const [loadingError, setLoadingError] = useState('');
  const [retry, setRetry] = useState(0);
  const pendingResult = job?.kind === 'evaluation' ? job.result_id : null;
  const reportId = selected?.taskId === task?.id ? selected?.id : pendingResult;
  const validHF = (name: string) => /^huggingface\/[A-Za-z0-9][A-Za-z0-9._-]{0,79}\/[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?::[A-Za-z0-9_-]{1,40})?$/.test(name);
  const validModel = (name: string) => /^(?:openai|gemini)\/[A-Za-z0-9][A-Za-z0-9._-]{0,89}$/.test(name) || validHF(name);
  const ready = Boolean(task?.snapshot_id && solution && testSuite && validationReport?.overall_verdict === 'VALIDATED');
  const disabledReason = !ready ? 'Validate the current task, reference solution and tests first.'
    : active ? 'Wait for the current job to finish.'
    : mode === 'agent' && !validModel(model.trim()) ? 'Enter the full model identifier, such as huggingface/organization/model. Replace organization/model with your actual Hugging Face model.'
    : mode === 'agent' && fallback.trim() && (!/^(openai|huggingface)\//.test(model.trim()) || !(validHF(fallback.trim()) || /^gemini\/[A-Za-z0-9][A-Za-z0-9._-]{0,89}$/.test(fallback.trim())))
      ? 'Enter a Gemini or Hugging Face fallback for an OpenAI or Hugging Face primary model.'
    : mode === 'agent' && comparison.trim() && !validModel(comparison.trim()) ? 'Enter a valid comparison model or clear the optional comparison field.'
    : mode === 'agent' && comparison.trim() && repeats * 2 > 6 ? 'Use at most three repeats when comparing two models.'
    : '';
  useEffect(() => {
    if (pendingResult) setSelected(null);
  }, [pendingResult]);
  useEffect(() => {
    const controller = new AbortController();
    setReport(null);
    setLoadingError('');
    if (reportId) workflow.artifact<EvaluationReport>('evaluation', reportId, controller.signal).then(result => {
      if (!controller.signal.aborted) setReport(result);
    }).catch(err => {
      if (!controller.signal.aborted) setLoadingError((err as Error).message);
    });
    return () => controller.abort();
  }, [reportId, retry]);
  const run = async () => {
    if (disabledReason) return;
    setError('');
    setSelected(null);
    try {
      const agents = mode === 'agent' ? [model.trim(), comparison.trim()].filter(Boolean).map((name, index) => ({
        label: index === 0 ? 'primary' : 'comparison', model: name, max_turns: 5,
        ...(index === 0 && fallback.trim() ? { fallback_model: fallback.trim() } : {}),
      })) : [];
      await queue('evaluation', { mode, repeats, agents });
    } catch (err) { setError((err as Error).message); }
  };
  const exportCSV = async (name: 'summary.csv' | 'trials.csv') => {
    if (!report) return;
    setError('');
    try { await workflow.download('/history/evaluation/' + report.id + '/' + name); }
    catch (err) { setError((err as Error).message); }
  };

  const displayed = report?.id === reportId ? report : null;
  return <>
    <PageHeader title="Agent Evaluation" description="Compare agent configurations over the same starting snapshot and verifier." />
    <section className="card workflow-panel">
      <h2>{task?.title || 'No task selected'}</h2>
      {!ready && <p>Run <Link to="/validation">Docker validation</Link> successfully on the current solution and tests to enable evaluation.</p>}
      <div className="workflow-row">
        <label>Run type<select value={mode} onChange={e => setMode(e.target.value as 'control' | 'agent')}>
          <option value="control">Harbor controls</option><option value="agent">Live coding agent</option>
        </select></label>
        <label>Independent repeats<select value={repeats} onChange={e => setRepeats(Number(e.target.value))}>
          {[1, 2, 3, 4, 5].map(n => <option key={n}>{n}</option>)}
        </select></label>
      </div>
      {mode === 'agent' ? <>
        <div className="workflow-row">
          <label>Model identifier<input value={model} maxLength={240} placeholder="huggingface/organization/model" onChange={e => { setModel(e.target.value); setFallback(''); }} /></label>
          <label>Comparison model, optional<input value={comparison} maxLength={240} placeholder="openai/your-second-model" onChange={e => setComparison(e.target.value)} /></label>
          {/^(openai|huggingface)\//.test(model) && <label>Fallback model, optional<input value={fallback} maxLength={240}
            placeholder="gemini/gemini-3.5-flash-lite" onChange={e => setFallback(e.target.value)} /></label>}
        </div>
        <p>Use gemini/gemini-3.5-flash-lite with your backend GEMINI_API_KEY, openai/model, or huggingface/organization/model.
          A configured fallback is selected when the primary API key is missing. Provider failures end the trial.
          A campaign supports up to six trials.</p>
      </> : <p>Controls check the task and verifier without making model requests. They do not count toward agent success rates.</p>}
      {disabledReason && <p id="evaluation-disabled-reason" role="status">{disabledReason}</p>}
      <button className="btn btn-primary" disabled={Boolean(disabledReason)}
        aria-describedby={disabledReason ? 'evaluation-disabled-reason' : undefined}
        onClick={() => void run()}>{mode === 'agent' ? 'Run agent evaluation' : 'Run controls'}</button>
      {error && <p role="alert">{error}</p>}
    </section>
    {task && <EvidenceHistory key={task.id} taskId={task.id} kind="evaluation" current={reportId || undefined}
      version={pendingResult} onSelect={id => setSelected({ taskId: task.id, id })} />}
    {loadingError && <p role="alert">{loadingError} <button className="btn btn-secondary" onClick={() => setRetry(n => n + 1)}>Retry</button></p>}
    {reportId && !displayed && !loadingError && <p role="status">Loading benchmark report…</p>}
    {displayed && <section className="card workflow-panel">
      <div className="workflow-row workflow-spread"><h2>Benchmark report</h2><StatusBadge status={displayed.state} /></div>
      <p>{displayed.mode === 'agent' ? 'Agent campaign' : 'Control campaign'} · <code>{displayed.id}</code></p>
      {displayed.failure && <p>Failure category {displayed.failure.replace(/_/g, ' ')}</p>}
      {displayed.detail && <p>{displayed.detail}</p>}
      <div className="workflow-row">
        <button className="btn btn-secondary" onClick={() => void exportCSV('summary.csv')}>Summary CSV</button>
        <button className="btn btn-secondary" onClick={() => void exportCSV('trials.csv')}>Trials CSV</button>
      </div>
      {displayed.package && <details className="snapshot-details">
        <summary>Artifact versions and environment</summary>
        <dl>
          <dt>Snapshot</dt><dd><code>{displayed.package.snapshot_id}</code></dd>
          <dt>Snapshot checksum</dt><dd><code>{displayed.package.snapshot_sha256}</code></dd>
          <dt>Starting commit</dt><dd>{displayed.package.starting_commit_sha || 'ZIP baseline'}</dd>
          <dt>Reference checksum</dt><dd><code>{displayed.package.solution_sha256}</code></dd>
          <dt>Test suite checksum</dt><dd><code>{displayed.package.test_suite_sha256}</code></dd>
          <dt>Package checksum</dt><dd><code>{displayed.package.package_sha256}</code></dd>
          <dt>Runtime image</dt><dd>{displayed.package.runtime_image}</dd>
          <dt>Harbor / Docker</dt><dd>{displayed.harbor_version || 'Unavailable'} / {displayed.docker_engine || 'Unavailable'}</dd>
        </dl>
      </details>}
      {displayed.metrics.map(metric => <div className="history-row" key={metric.label}>
        <h3>{metric.label} · {metric.scored}/{metric.scheduled} scored</h3>
        <dl className="metrics-grid">
          <div><dt>Verified success</dt><dd>{displayRate(metric.verified_success)}</dd></div>
          <div><dt>Build success</dt><dd>{displayRate(metric.build_success)}</dd></div>
          <div><dt>Feature tests</dt><dd>{displayRate(metric.feature_pass)}</dd></div>
          <div><dt>Regression tests</dt><dd>{displayRate(metric.regression_pass)}</dd></div>
          <div><dt>Mean duration</dt><dd>{metric.mean_agent_seconds === null ? 'Unavailable' : metric.mean_agent_seconds.toFixed(2) + ' s'}</dd></div>
          <div><dt>Input / output tokens</dt><dd>{metric.input_tokens_total ?? '–'} / {metric.output_tokens_total ?? '–'}</dd></div>
          <div><dt>Reported cost</dt><dd>{metric.cost_usd_total === null ? 'Unavailable' : '$' + metric.cost_usd_total.toFixed(4)}</dd></div>
          <div><dt>Success 95% interval</dt><dd>{metric.verified_success.wilson95?.map(x => (x * 100).toFixed(1) + '%').join('–') || 'Unavailable'}</dd></div>
        </dl>
        {Object.entries(metric.failure_counts).map(([failure, count]) => <p key={failure}>{failure.replace(/_/g, ' ')} · {count}</p>)}
      </div>)}
      {displayed.oracle && <TrialEvidence trial={displayed.oracle} id={displayed.id} />}
      {displayed.baseline_control && <TrialEvidence trial={displayed.baseline_control} id={displayed.id} />}
      {displayed.trials.map(trial => <TrialEvidence trial={trial} id={displayed.id} key={trial.label + '-' + trial.attempt} />)}
    </section>}
  </>;
}
