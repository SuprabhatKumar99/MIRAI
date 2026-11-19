import { requestBlob, requestJson } from './api';
import type { HistoryItem, Job, JobOptions, Kind, Project, TaskDetail, TaskList } from '../types/workflow';
import type { RepoAnalysisResponse } from '../types/repo';

export const workflow = {
  task: (id: string, signal?: AbortSignal) => requestJson<TaskDetail>('/tasks/' + encodeURIComponent(id), { signal }),
  snapshot: (id: string, signal?: AbortSignal) => requestJson<RepoAnalysisResponse>('/snapshots/' + encodeURIComponent(id), { signal }),
  tasks: (params: URLSearchParams, signal?: AbortSignal) => requestJson<TaskList>('/tasks/?' + params, { signal }),
  projects: (offset: number, signal?: AbortSignal) => requestJson<{ projects: Project[] }>('/projects?limit=10&offset=' + offset, { signal }),
  queue: (id: string, revision: number, kind: Kind, request_key: string, options: JobOptions) => requestJson<Job>('/tasks/' + id + '/jobs', {
    method: 'POST', body: JSON.stringify({ kind, revision, request_key, ...options }),
  }),
  job: (id: string, signal?: AbortSignal) => requestJson<Job>('/jobs/' + id, { signal }),
  cancel: (id: string) => requestJson<Job>('/jobs/' + id, { method: 'DELETE' }),
  history: (id: string, kind: Kind, offset: number, signal?: AbortSignal) => requestJson<{ items: HistoryItem[] }>(
    '/tasks/' + id + '/history/' + kind + '?limit=20&offset=' + offset, { signal },
  ),
  artifact: <T,>(kind: Kind, id: string, signal?: AbortSignal) => requestJson<T>('/history/' + kind + '/' + id, { signal }),
  download: async (path: string) => {
    const { blob, filename } = await requestBlob(path);
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  },
};
