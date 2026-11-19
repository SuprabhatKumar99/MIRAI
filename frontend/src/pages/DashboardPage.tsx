import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { StatusBadge } from '../components/StatusBadge';
import { useProject } from '../context/ProjectContext';
import { workflow } from '../services/workflowService';
import type { Project, TaskList } from '../types/workflow';

const pageSize = 12;
export function DashboardPage() {
  const { task, selectTask, setRepoAnalysis, job } = useProject();
  const navigate = useNavigate();
  const [title, setTitle] = useState('');
  const [difficulty, setDifficulty] = useState('');
  const [project, setProject] = useState('');
  const [offset, setOffset] = useState(0);
  const [projectOffset, setProjectOffset] = useState(0);
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [records, setRecords] = useState<TaskList | null>(null);
  const [error, setError] = useState('');
  const [projectError, setProjectError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setProjects(null);
    setProjectError('');
    workflow.projects(projectOffset, controller.signal).then(result => {
      if (!controller.signal.aborted) setProjects(result.projects);
    }).catch(err => {
      if (!controller.signal.aborted) setProjectError((err as Error).message);
    });
    return () => controller.abort();
  }, [projectOffset, retry]);
  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ limit: String(pageSize), offset: String(offset) });
    if (title.trim()) params.set('q', title.trim());
    if (difficulty) params.set('difficulty', difficulty);
    if (project) params.set('project_id', project);
    setRecords(null);
    setError('');
    const timer = window.setTimeout(() => {
      workflow.tasks(params, controller.signal).then(result => {
        if (!controller.signal.aborted) setRecords(result);
      }).catch(err => {
        if (!controller.signal.aborted) setError((err as Error).message);
      });
    }, 220);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [title, difficulty, project, offset, retry, task?.id, job?.result_id]);

  return <>
    <PageHeader title="MIRAI Dashboard" description="Open saved work, inspect validation evidence, or start with a repository."
      actions={<Link className="btn btn-primary" to="/new-project">New project</Link>} />
    <section className="card workflow-panel">
      <div className="workflow-row workflow-spread"><h2>Saved tasks</h2>{records && <span>{records.total} {records.total === 1 ? 'task' : 'tasks'}</span>}</div>
      <div className="workflow-row">
        <label>Task title<input type="search" value={title} maxLength={120} onChange={e => { setTitle(e.target.value); setOffset(0); }} /></label>
        <label>Difficulty<select value={difficulty} onChange={e => { setDifficulty(e.target.value); setOffset(0); }}>
          <option value="">All</option><option value="easy">Easy</option><option value="medium">Medium</option><option value="hard">Hard</option>
        </select></label>
        {project && <button className="btn btn-secondary" onClick={() => { setProject(''); setOffset(0); }}>Clear project filter</button>}
      </div>
      {error && <p role="alert">{error} <button className="btn btn-secondary" onClick={() => setRetry(n => n + 1)}>Retry</button></p>}
      {!records && !error && <p role="status">Loading tasks…</p>}
      {records?.tasks.length === 0 && <p>No tasks match these filters.</p>}
      {records?.tasks.map((entry, index) => <div className="workflow-row workflow-spread history-row" key={entry.id}>
        <div>
          <button className="btn btn-subtle" onClick={() => { selectTask(entry.id); navigate('/task-generator'); }}>{entry.title}</button>
          <p>{entry.language} · {entry.category} · {entry.difficulty}</p>
        </div>
        <StatusBadge status={records.states[index]?.verdict || 'Awaiting validation'} />
      </div>)}
      {(offset > 0 || (records?.total || 0) > pageSize) && <div className="workflow-row">
        <button className="btn btn-secondary" disabled={offset === 0} onClick={() => setOffset(n => n - pageSize)}>Previous tasks</button>
        <button className="btn btn-secondary" disabled={!records || offset + pageSize >= records.total} onClick={() => setOffset(n => n + pageSize)}>Next tasks</button>
      </div>}
    </section>
    <section className="card workflow-panel">
      <h2>Saved repositories</h2>
      {projectError && <p role="alert">{projectError} <button className="btn btn-secondary" onClick={() => setRetry(n => n + 1)}>Retry</button></p>}
      {!projects && !projectError && <p role="status">Loading repositories…</p>}
      {projects?.length === 0 && <p>{projectOffset ? 'End of repositories.' : 'Add a ZIP archive or public GitHub repository to get started.'}</p>}
      {projects?.map(entry => <div className="workflow-row workflow-spread history-row" key={entry.id}>
        <div>
          <strong>{entry.name}</strong>
          <p>{entry.snapshot.detected_languages.join(', ') || 'Unknown language'} · {entry.snapshot.file_count} files</p>
        </div>
        <div className="workflow-row">
          <button className="btn btn-secondary" onClick={() => { setProject(entry.id); setOffset(0); }}>Filter tasks</button>
          <button className="btn btn-secondary" onClick={() => { setRepoAnalysis(entry.snapshot); navigate('/analysis'); }}>Open snapshot</button>
        </div>
      </div>)}
      {(projectOffset > 0 || projects?.length === 10) && <div className="workflow-row">
        <button className="btn btn-secondary" disabled={!projectOffset} onClick={() => setProjectOffset(n => n - 10)}>Previous repositories</button>
        <button className="btn btn-secondary" disabled={projects?.length !== 10} onClick={() => setProjectOffset(n => n + 10)}>Next repositories</button>
      </div>}
    </section>
  </>;
}
