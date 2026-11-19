import React, { useEffect, useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import {
  ShieldCheck,
  ShieldAlert,
  ArrowRight,
  Play,
  XCircle,
  AlertCircle,
} from 'lucide-react';
import { PageHeader } from '../components/PageHeader';
import { LoadingState } from '../components/LoadingState';
import { ErrorState } from '../components/ErrorState';
import { StatusBadge } from '../components/StatusBadge';
import { useProject } from '../context/ProjectContext';

import type { GateName, ValidationReport } from '../types/validation';
import { workflow } from '../services/workflowService';
import { EvidenceHistory } from '../components/EvidenceHistory';
import { EvidenceLog } from '../components/EvidenceLog';

const gateLabels: Record<GateName, string> = {
  baseline: 'Original repository tests',
  reference: 'Reference application and compile',
  feature: 'Generated feature tests',
  regression: 'Regression tests',
  negative_control: 'Negative controls',
};

export const ValidationPage: React.FC = () => {
  const navigate = useNavigate();
  const { task, solution, testSuite, validationReport: currentReport, queue, active, job } = useProject();

  const isLoading = active && job?.kind === 'validation';
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const [pastId, setPastId] = useState<string | undefined>();
  const [past, setPast] = useState<ValidationReport | null>(null);
  const [pastError, setPastError] = useState('');
  const validationReport = pastId ? past : currentReport;
  useEffect(() => { setPastId(undefined); }, [task?.id]);
  useEffect(() => {
    const controller = new AbortController();
    setPast(null);
    setPastError('');
    if (pastId) workflow.artifact<ValidationReport>('validation', pastId, controller.signal).then(result => {
      if (!controller.signal.aborted) setPast(result);
    }).catch(error => {
      if (!controller.signal.aborted) setPastError((error as Error).message);
    });
    return () => controller.abort();
  }, [pastId]);

  const handleValidate = async () => {
    if (!task || !solution || !testSuite || active) return;
    setErrorMessage(null);
    try {
      setPastId(undefined);
      await queue('validation');
      
    } catch (error) {
      setErrorMessage((error as Error).message);
    }
  };

  return (
    <div>
      <PageHeader
        title="Validation & Integrity Engine"
        description="Run the build and tests in Docker against the immutable starting repository."
      />

      {/* Dependency check banner */}
      {!task || !solution || !testSuite ? (
        <div className="card" style={{ padding: '32px 24px', textAlign: 'center' }}>
          <AlertCircle size={28} style={{ color: 'var(--warning)', margin: '0 auto 12px' }} />
          <h3 style={{ fontSize: '15px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '6px' }}>
            Incomplete Task Package
          </h3>
          <p style={{ fontSize: '13px', color: 'var(--text-secondary)', maxWidth: '480px', margin: '0 auto 20px' }}>
            Full package validation requires a Task Specification, a Reference Solution, and an Automated Test Suite.
          </p>
          <div style={{ display: 'flex', gap: '8px', justifyContent: 'center' }}>
            {!task && (
              <Link to="/task-generator" className="btn btn-secondary">
                Generate Task
              </Link>
            )}
            {!solution && (
              <Link to="/solution" className="btn btn-secondary">
                Synthesize Solution
              </Link>
            )}
            {!testSuite && (
              <Link to="/test-suite" className="btn btn-secondary">
                Synthesize Tests
              </Link>
            )}
          </div>
        </div>
      ) : (
        <>
          {/* Action Bar */}
          <div className="card" style={{ padding: '20px', marginBottom: '24px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '12px' }}>
              <div>
                <div style={{ fontSize: '11px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                  Validation Candidate
                </div>
                <h3 style={{ fontSize: '16px', fontWeight: 600, color: 'var(--text-primary)' }}>
                  {task.title}
                </h3>
                <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '2px' }}>
                  Solution files: {solution.files.length} · Feature tests: {testSuite.total_feature_tests} · Regression checks: {testSuite.total_regression_tests}
                </div>
              </div>

              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <button
                  onClick={handleValidate}
                  disabled={active || !task || !solution || !testSuite}
                  className="btn btn-primary"
                >
                  <Play size={14} />
                  <span>{validationReport ? 'Re-run Validation Gates' : 'Run Validation Gates'}</span>
                </button>

                {validationReport && (
                  <button
                    onClick={() => navigate('/export')}
                    className="btn btn-secondary"
                  >
                    <span>Proceed to Harbor Export</span>
                    <ArrowRight size={14} />
                  </button>
                )}
              </div>
            </div>
          </div>

          {/* Loading & Error States */}
          {isLoading && (
            <div className="card" style={{ padding: '24px', marginBottom: '24px' }}>
              <LoadingState
                message="Evaluating task package through validation gates..."
                subtext="Restoring the snapshot and running each required gate in an isolated container."
              />
            </div>
          )}

          {errorMessage && (
            <div style={{ marginBottom: '24px' }}>
              <ErrorState
                title="Validation Failed"
                message={errorMessage}
                onRetry={handleValidate}
              />
            </div>
          )}

        </>
      )}

      {task && <EvidenceHistory key={task.id} taskId={task.id} kind="validation" current={pastId || currentReport?.id}
            onSelect={setPastId} version={job?.result_id} />}
          {pastId && <div className="workflow-row workflow-hint">
            <strong>Historical validation</strong>
            <button className="btn btn-secondary" onClick={() => setPastId(undefined)}>Show current validation</button>
            {!past && !pastError && <span role="status">Loading report…</span>}
          </div>}
          {pastError && <ErrorState message={pastError} />}

          {/* Validation Report View */}
          {task && validationReport && !isLoading && (
            <div>
              {/* Verdict Summary Card */}
              <div
                className="card"
                style={{
                  padding: '24px',
                  marginBottom: '24px',
                  borderLeft: `4px solid ${
                    validationReport.overall_verdict === 'VALIDATED'
                      ? 'var(--success)'
                      : validationReport.overall_verdict === 'REJECTED'
                      ? 'var(--danger)'
                      : 'var(--warning)'
                  }`,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px', flexWrap: 'wrap', gap: '8px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    {validationReport.overall_verdict === 'VALIDATED' ? (
                      <ShieldCheck size={22} style={{ color: 'var(--success)' }} />
                    ) : validationReport.overall_verdict === 'REJECTED' ? (
                      <XCircle size={22} style={{ color: 'var(--danger)' }} />
                    ) : (
                      <ShieldAlert size={22} style={{ color: 'var(--warning)' }} />
                    )}
                    <div>
                      <h3 style={{ fontSize: '16px', fontWeight: 600, color: 'var(--text-primary)' }}>
                        Overall Verdict: {validationReport.overall_verdict}
                      </h3>
                      <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                        Execution mode: <code className="code-inline">{validationReport.execution_mode}</code> · Duration: {validationReport.execution_duration_ms}ms
                      </div>
                    </div>
                  </div>

                  <StatusBadge status={validationReport.overall_verdict} />
                </div>

                {/* Truthful explanation banner */}
                <div
                  style={{
                    padding: '12px 14px',
                    borderRadius: 'var(--radius-md)',
                    backgroundColor: validationReport.overall_verdict === 'NOT_VALIDATED' ? 'var(--warning-light)' : 'var(--bg-muted)',
                    fontSize: '12.5px',
                    color: validationReport.overall_verdict === 'NOT_VALIDATED' ? 'var(--warning-text)' : 'var(--text-secondary)',
                    lineHeight: 1.5,
                  }}
                >
                  {validationReport.evidence_location
                    ? <>Evidence <code className="code-inline">{validationReport.evidence_location}</code></>
                    : 'No Docker evidence was recorded.'}
                </div>
              </div>

              {Boolean(validationReport.gates?.length) && (
                <div className="card" style={{ padding: '20px', marginBottom: '24px' }}>
                  <h4 style={{ fontSize: '14px', marginBottom: '16px' }}>Validation gates</h4>
                  <div style={{ display: 'grid', gap: '14px' }}>
                    {validationReport.gates?.map((gate) => (
                      <div key={gate.gate}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', gap: '12px' }}>
                          <strong>{gateLabels[gate.gate]}</strong>
                          <span className={`badge ${gate.status === 'passed' ? 'badge-success' : gate.status === 'failed' ? 'badge-danger' : 'badge-default'}`}>
                            {gate.status.replace('_', ' ').toUpperCase()}
                          </span>
                        </div>
                        {gate.failure_category && <p>{gate.failure_category.replace(/_/g, " ")}</p>}
                        {gate.detail && <p style={{ fontSize: '12px' }}>{gate.detail}</p>}
                        {gate.runs.map((run, index) => (
                          <div key={index} style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                            {run.timed_out ? 'Timed out' : run.oom_killed ? 'Memory limit reached' : run.exit_code === null ? 'No exit code' : `Exit ${run.exit_code}`}
                            {' · '}{run.duration_ms}ms
                                                        {run.commands?.map((command, commandIndex) => (
                              <pre key={commandIndex} className="evidence-command">{command.argv.join(' ')}</pre>
                            ))}
                            {run.log_location && <EvidenceLog key={run.log_location}
                              path={'/history/validation/' + validationReport.id + '/logs/' + encodeURIComponent(run.log_location.split('/').pop() || '')} />}
                          </div>
                        ))}
                      </div>
                    ))}
                  </div>
                </div>
              )}
              {/* Rejection / Status Reasons Trail */}
              {validationReport.rejection_reasons.length > 0 && (
                <div className="card" style={{ padding: '18px' }}>
                  <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: '8px' }}>
                    Audit & Reason Log
                  </div>
                  <ul style={{ paddingLeft: '18px', fontSize: '12.5px', color: 'var(--text-secondary)', display: 'flex', flexDirection: 'column', gap: '6px' }}>
                    {validationReport.rejection_reasons.map((reason, idx) => (
                      <li key={idx}>{reason}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
    </div>
  );
};
