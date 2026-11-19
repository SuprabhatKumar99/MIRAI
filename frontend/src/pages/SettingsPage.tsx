import React, { useState } from 'react';
import {
  Server,
  RotateCcw,
  CheckCircle2,
  AlertCircle,
  Cpu,
} from 'lucide-react';
import { PageHeader } from '../components/PageHeader';
import { ConfirmationDialog } from '../components/ConfirmationDialog';
import { useProject } from '../context/ProjectContext';

export const SettingsPage: React.FC = () => {
  const { backendHealth, checkBackendHealth, resetProject } = useProject();
  const [isResetDialogOpen, setIsResetDialogOpen] = useState(false);
  const [isProbing, setIsProbing] = useState(false);

  const handleProbe = async () => {
    setIsProbing(true);
    await checkBackendHealth();
    setIsProbing(false);
  };

  const handleConfirmReset = () => {
    resetProject();
    setIsResetDialogOpen(false);
  };

  return (
    <div>
      <PageHeader
        title="Settings & System Status"
        description="Inspect backend connectivity, runtime configuration, and manage local session state."
      />

      <div style={{ display: 'flex', flexDirection: 'column', gap: '20px', maxWidth: '720px' }}>
        {/* Backend Connectivity Card */}
        <div className="card" style={{ padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '16px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <Server size={18} style={{ color: 'var(--accent-primary)' }} />
              <div>
                <h3 style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
                  Backend Server Connection
                </h3>
                <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                  Target: {import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000 (Vite Proxy)'}
                </div>
              </div>
            </div>

            <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  padding: '4px 10px',
                  borderRadius: 'var(--radius-full)',
                  backgroundColor: backendHealth === 'online' ? 'var(--success-light)' : 'var(--danger-light)',
                  border: `1px solid ${backendHealth === 'online' ? 'var(--success-border)' : 'var(--danger-border)'}`,
                  color: backendHealth === 'online' ? 'var(--success-text)' : 'var(--danger-text)',
                  fontSize: '11px',
                  fontWeight: 500,
                }}
              >
                {backendHealth === 'online' ? <CheckCircle2 size={12} /> : <AlertCircle size={12} />}
                <span>{backendHealth === 'online' ? 'Connected' : 'Disconnected'}</span>
              </div>

              <button
                onClick={handleProbe}
                disabled={isProbing}
                className="btn btn-secondary"
                style={{ fontSize: '12px', padding: '6px 12px' }}
              >
                <span>{isProbing ? 'Probing...' : 'Test Connection'}</span>
              </button>
            </div>
          </div>

          <p style={{ fontSize: '12.5px', color: 'var(--text-secondary)', lineHeight: 1.5 }}>
            All operations (repository analysis, AI synthesis, Docker gate validation, and Harbor export) communicate with this backend via type-safe REST endpoints.
          </p>
        </div>

        {/* Runtime Standards Card */}
        <div className="card" style={{ padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '14px' }}>
            <Cpu size={18} style={{ color: 'var(--info)' }} />
            <h3 style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
              Evaluation Engine Standards
            </h3>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', fontSize: '12.5px', color: 'var(--text-secondary)' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid var(--border-default)' }}>
              <span>Target Benchmark Format</span>
              <strong style={{ color: 'var(--text-primary)' }}>Harbor Schema 1.4</strong>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid var(--border-default)' }}>
              <span>Evaluation Verifier Protocol</span>
              <strong style={{ color: 'var(--text-primary)' }}>/logs/verifier/reward.txt (1.0 / 0.0)</strong>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px solid var(--border-default)' }}>
              <span>Code Execution Boundary</span>
              <strong style={{ color: 'var(--text-primary)' }}>Docker containers</strong>
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0' }}>
              <span>AI Provider & Model</span>
              <strong style={{ color: 'var(--text-primary)' }}>Google Gemini · Backend configured</strong>
            </div>
          </div>
        </div>

        {/* Clear Selection State */}
        <div className="card" style={{ padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div>
              <h3 style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
                Clear Active Selection
              </h3>
              <p style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '2px' }}>
                Deselects the active task and repository. Saved work stays in the dashboard.
              </p>
            </div>

            <button
              onClick={() => setIsResetDialogOpen(true)}
              className="btn btn-secondary"
              style={{ color: 'var(--danger-text)', borderColor: 'var(--danger-border)' }}
            >
              <RotateCcw size={13} />
              <span>Clear Selection</span>
            </button>
          </div>
        </div>
      </div>

      {/* Confirmation Dialog */}
      <ConfirmationDialog
        isOpen={isResetDialogOpen}
        title="Clear active selection?"
        message="You can reopen saved work from the dashboard."
        confirmLabel="Clear Selection"
        cancelLabel="Keep Active"
        isDestructive
        onConfirm={handleConfirmReset}
        onCancel={() => setIsResetDialogOpen(false)}
      />
    </div>
  );
};

