import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { taskService } from '../services/taskService';
import { workflow } from '../services/workflowService';
import type { RepoAnalysisResponse } from '../types/repo';
import type { TaskResponse } from '../types/task';
import type { Job, JobOptions, Kind, TaskDetail } from '../types/workflow';
import { isActive } from '../types/workflow';

const selectionKey = 'ctf.selection.v1';
interface Selection { task?: string; snapshot?: string }
function readSelection(): Selection {
  try {
    const value = JSON.parse(localStorage.getItem(selectionKey) || '{}');
    return {
      task: typeof value?.task === 'string' ? value.task : undefined,
      snapshot: typeof value?.snapshot === 'string' ? value.snapshot : undefined,
    };
  } catch { return {}; }
}

interface ProjectContextValue {
  repoAnalysis: RepoAnalysisResponse | null;
  task: TaskResponse | null;
  solution: TaskDetail['solution'];
  testSuite: TaskDetail['test_suite'];
  validationReport: TaskDetail['validation'];
  savedExport: TaskDetail['export'];
  loading: boolean;
  loadError: string;
  refresh: () => void;
  setRepoAnalysis: (analysis: RepoAnalysisResponse) => void;
  setTask: (task: TaskResponse) => void;
  selectTask: (id: string) => void;
  resetProject: () => void;
  job: Job | null;
  active: boolean;
  queue: (kind: Kind, options?: JobOptions) => Promise<void>;
  cancelJob: () => Promise<void>;
  backendHealth: 'online' | 'offline' | 'checking';
  checkBackendHealth: () => Promise<void>;
}

const ProjectContext = createContext<ProjectContextValue | undefined>(undefined);
export const ProjectProvider: React.FC<React.PropsWithChildren> = ({ children }) => {
  const [selection, setSelection] = useState(readSelection);
  const selected = useRef(selection);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [snapshot, setSnapshot] = useState<RepoAnalysisResponse | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const inFlight = useRef(false);
  const intent = useRef<{ body: string; key: string } | null>(null);
  const [loading, setLoading] = useState(() => Boolean(selection.task || selection.snapshot));
  const [loadError, setLoadError] = useState('');
  const [reload, setReload] = useState(0);
  const [backendHealth, setBackendHealth] = useState<ProjectContextValue['backendHealth']>('checking');
  const refresh = useCallback(() => setReload(value => value + 1), []);

  const checkBackendHealth = useCallback(async () => {
    try {
      const res = await taskService.checkHealth();
      setBackendHealth(res.status === 'healthy' ? 'online' : 'offline');
    } catch { setBackendHealth('offline'); }
  }, []);

  useEffect(() => {
    void checkBackendHealth();
    const interval = setInterval(checkBackendHealth, 30000);
    return () => clearInterval(interval);
  }, [checkBackendHealth]);

  const select = (next: Selection) => {
    selected.current = next;
    setSelection(next);
    setDetail(null);
    setSnapshot(null);
    setJob(null);
    setLoadError('');
    setLoading(Boolean(next.task || next.snapshot));
    try { localStorage.setItem(selectionKey, JSON.stringify(next)); } catch { /* Selection can stay in memory. */ }
  };

  useEffect(() => {
    if (!selection.task && !selection.snapshot) return;
    const controller = new AbortController();
    setLoading(true);
    setLoadError('');
    const fetchSelection = async () => {
      try {
        if (selection.task) {
          const saved = await workflow.task(selection.task, controller.signal);
          if (controller.signal.aborted || selected.current !== selection) return;
          setDetail(saved);
          setJob(saved.jobs[0] ?? null);
        } else if (selection.snapshot) {
          const saved = await workflow.snapshot(selection.snapshot, controller.signal);
          if (!controller.signal.aborted && selected.current === selection) setSnapshot(saved);
        }
      } catch (error) {
        if (!controller.signal.aborted && selected.current === selection) setLoadError((error as Error).message);
      } finally {
        if (!controller.signal.aborted && selected.current === selection) setLoading(false);
      }
    };
    void fetchSelection();
    const sync = () => { if (document.visibilityState === 'visible') refresh(); };
    window.addEventListener('focus', sync);
    return () => {
      controller.abort();
      window.removeEventListener('focus', sync);
    };
  }, [selection, reload, refresh]);

  const activeJobId = job && isActive(job) ? job.id : null;
  useEffect(() => {
    if (!activeJobId) return;
    const controller = new AbortController();
    let timer: number;
    let delay = 1200;
    const poll = async () => {
      try {
        const current = await workflow.job(activeJobId, controller.signal);
        if (controller.signal.aborted || selected.current !== selection) return;
        setJob(current);
        setLoadError('');
        if (!isActive(current)) {
          refresh();
          return;
        }
        delay = 1200;
      } catch (error) {
        if (controller.signal.aborted || selected.current !== selection) return;
        setLoadError((error as Error).message);
        delay = Math.min(delay * 2, 15000);
      }
      timer = window.setTimeout(poll, delay);
    };
    timer = window.setTimeout(poll, delay);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [activeJobId, selection, refresh]);

  const queue = async (kind: Kind, options: JobOptions = {}) => {
    if (!detail || inFlight.current || activeJobId) return;
    inFlight.current = true;
    setSubmitting(true);
    const requestSelection = selected.current;
    const body = JSON.stringify([detail.task.id, detail.revision, kind, options]);
    if (intent.current?.body !== body) intent.current = { body, key: crypto.randomUUID() };
    try {
      const current = await workflow.queue(detail.task.id, detail.revision, kind, intent.current.key, options);
      intent.current = null;
      if (selected.current === requestSelection) {
        setJob(current);
        if (!isActive(current)) refresh();
      }
    } catch (error) {
      if ((error as { statusCode?: number }).statusCode === 409) refresh();
      throw error;
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  };

  const cancelJob = async () => {
    if (!activeJobId) return;
    const requestSelection = selected.current;
    const canceled = await workflow.cancel(activeJobId);
    if (selected.current !== requestSelection) return;
    setJob(canceled);
    if (!isActive(canceled)) refresh();
  };

  return (
    <ProjectContext.Provider value={{
      repoAnalysis: detail?.snapshot ?? snapshot, task: detail?.task ?? null,
      solution: detail?.solution ?? null, testSuite: detail?.test_suite ?? null,
      validationReport: detail?.validation ?? null, savedExport: detail?.export ?? null,
      setRepoAnalysis: analysis => select({ snapshot: analysis.snapshot_id ?? undefined }),
      setTask: task => select({ task: task.id }), selectTask: id => select({ task: id }),
      resetProject: () => select({}), refresh, loading, loadError, job,
      active: submitting || Boolean(activeJobId), queue, cancelJob, backendHealth, checkBackendHealth,
    }}>
      {children}
    </ProjectContext.Provider>
  );
};

export const useProject = (): ProjectContextValue => {
  const context = useContext(ProjectContext);
  if (!context) throw new Error('useProject must be used within a ProjectProvider');
  return context;
};

