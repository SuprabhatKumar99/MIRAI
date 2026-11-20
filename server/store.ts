import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import type {
  EvaluationReport,
  HistoryItem,
  Job,
  Kind,
  RepoAnalysisResponse,
  SavedExport,
  SnapshotRecord,
  SolutionResponse,
  TaskResponse,
  TestSuiteResponse,
  ValidationReport,
} from './types.ts';

export interface ProjectRecord {
  id: string;
  name: string;
  owner_id?: string | null;
  snapshot: RepoAnalysisResponse;
  created_at: string;
}

export interface ArtifactRecord {
  id: string;
  task_id: string;
  owner_id?: string | null;
  kind: Kind;
  body: unknown;
  status: string | null;
  solution_id: string | null;
  suite_id: string | null;
  created_at: string;
}

export interface TaskRecord {
  task: TaskResponse;
  owner_id?: string | null;
  project_id: string | null;
  revision: number;
  solution_id: string | null;
  suite_id: string | null;
  validation_id: string | null;
  export_id: string | null;
  updated_at: string;
}

interface PersistedState {
  snapshots: Array<[string, SnapshotRecord]>;
  projects: Array<[string, ProjectRecord]>;
  tasks: Array<[string, TaskRecord]>;
  artifacts: Array<[string, ArtifactRecord]>;
  jobs: Array<[string, Job]>;
  validationLogs: Array<[string, string]>;
  evaluationLogs: Array<[string, string]>;
  exportMetas: Array<[string, SavedExport]>;
}

export function sha256Hex(content: string | Buffer): string {
  return crypto.createHash('sha256').update(content).digest('hex');
}

export function randomHex(bytes = 16): string {
  return crypto.randomBytes(bytes).toString('hex');
}

class DiskBackedWorkflowStore {
  private readonly stateDir: string;
  private readonly stateFile: string;
  private readonly exportsDir: string;

  snapshots = new Map<string, SnapshotRecord>();
  projects = new Map<string, ProjectRecord>();
  tasks = new Map<string, TaskRecord>();
  artifacts = new Map<string, ArtifactRecord>();
  jobs = new Map<string, Job>();
  requestKeys = new Map<string, string>();
  exportArchives = new Map<string, { buffer: Buffer; meta: SavedExport }>();
  validationLogs = new Map<string, string>();
  evaluationLogs = new Map<string, string>();

  constructor() {
    this.stateDir = path.resolve(process.env.CTF_STATE_DIR?.trim() || path.join(process.cwd(), '.mirai-state'));
    this.stateFile = path.join(this.stateDir, 'state.json');
    this.exportsDir = path.join(this.stateDir, 'exports');
    this.hydrate();
  }

  private hydrate() {
    try {
      if (!fs.existsSync(this.stateFile)) return;
      const raw = fs.readFileSync(this.stateFile, 'utf8');
      const data = JSON.parse(raw) as Partial<PersistedState>;

      if (Array.isArray(data.snapshots)) {
        for (const [k, v] of data.snapshots) this.snapshots.set(k, v);
      }
      if (Array.isArray(data.projects)) {
        for (const [k, v] of data.projects) this.projects.set(k, v);
      }
      if (Array.isArray(data.tasks)) {
        for (const [k, v] of data.tasks) this.tasks.set(k, v);
      }
      if (Array.isArray(data.artifacts)) {
        for (const [k, v] of data.artifacts) this.artifacts.set(k, v);
      }
      if (Array.isArray(data.jobs)) {
        for (const [k, v] of data.jobs) {
          if (v.state === 'queued' || v.state === 'running') {
            v.state = 'interrupted';
            v.error = v.error || 'Server restarted while job was in progress.';
          }
          this.jobs.set(k, v);
        }
      }
      if (Array.isArray(data.validationLogs)) {
        for (const [k, v] of data.validationLogs) this.validationLogs.set(k, v);
      }
      if (Array.isArray(data.evaluationLogs)) {
        for (const [k, v] of data.evaluationLogs) this.evaluationLogs.set(k, v);
      }
      if (Array.isArray(data.exportMetas)) {
        for (const [id, meta] of data.exportMetas) {
          const zipPath = path.join(this.exportsDir, `${id}.zip`);
          if (fs.existsSync(zipPath)) {
            this.exportArchives.set(id, { buffer: fs.readFileSync(zipPath), meta });
          }
        }
      }
    } catch (err) {
      console.warn('[MIRAI Store] Could not hydrate persisted state:', err);
    }
  }

  private persist() {
    try {
      fs.mkdirSync(this.stateDir, { recursive: true });
      const payload: PersistedState = {
        snapshots: Array.from(this.snapshots.entries()),
        projects: Array.from(this.projects.entries()),
        tasks: Array.from(this.tasks.entries()),
        artifacts: Array.from(this.artifacts.entries()),
        jobs: Array.from(this.jobs.entries()),
        validationLogs: Array.from(this.validationLogs.entries()),
        evaluationLogs: Array.from(this.evaluationLogs.entries()),
        exportMetas: Array.from(this.exportArchives.entries()).map(([id, entry]) => [id, entry.meta]),
      };
      const tmpFile = `${this.stateFile}.${process.pid}.tmp`;
      fs.writeFileSync(tmpFile, JSON.stringify(payload), 'utf8');
      fs.renameSync(tmpFile, this.stateFile);
    } catch (err) {
      console.warn('[MIRAI Store] Could not persist state to disk:', err);
    }
  }

  /**
   * Assign pre-authentication legacy records (owner_id == null) to a verified user once
   * so existing workspace progress is preserved under their account ID.
   */
  claimLegacyRecordsForOwner(ownerId: string): boolean {
    let changed = false;
    for (const snap of this.snapshots.values()) {
      if (!snap.owner_id) {
        snap.owner_id = ownerId;
        changed = true;
      }
    }
    for (const proj of this.projects.values()) {
      if (!proj.owner_id) {
        proj.owner_id = ownerId;
        changed = true;
      }
    }
    for (const rec of this.tasks.values()) {
      if (!rec.owner_id) {
        rec.owner_id = ownerId;
        changed = true;
      }
    }
    for (const art of this.artifacts.values()) {
      if (!art.owner_id) {
        art.owner_id = ownerId;
        changed = true;
      }
    }
    for (const job of this.jobs.values()) {
      if (!job.owner_id) {
        job.owner_id = ownerId;
        changed = true;
      }
    }
    if (changed) this.persist();
    return changed;
  }

  saveSnapshot(
    analysis: RepoAnalysisResponse,
    files: Record<string, string>,
    ownerId: string
  ): RepoAnalysisResponse {
    const contentSha = analysis.content_sha256 || sha256Hex(JSON.stringify(files));
    const existingSnap = analysis.snapshot_id ? this.snapshots.get(analysis.snapshot_id) : undefined;
    const canReuseId =
      Boolean(analysis.snapshot_id) && (!existingSnap || existingSnap.owner_id === ownerId);
    const snapshotId = canReuseId
      ? (analysis.snapshot_id as string)
      : `snap_${sha256Hex(`${ownerId}:${contentSha}`).slice(0, 16)}`;

    const normalized: RepoAnalysisResponse = {
      ...analysis,
      snapshot_id: snapshotId,
      content_sha256: contentSha,
      retrieved_at: analysis.retrieved_at || new Date().toISOString(),
      analysis_status: 'completed',
    };

    const projectId = `proj_${sha256Hex(`${ownerId}:${normalized.repository_identity || normalized.project_name}`).slice(0, 12)}`;
    this.snapshots.set(snapshotId, { analysis: normalized, files, projectId, owner_id: ownerId });
    this.projects.set(projectId, {
      id: projectId,
      name: normalized.project_name,
      owner_id: ownerId,
      snapshot: normalized,
      created_at: normalized.retrieved_at || new Date().toISOString(),
    });
    this.persist();

    return normalized;
  }

  getSnapshot(snapshotId: string, ownerId?: string): SnapshotRecord | undefined {
    const snap = this.snapshots.get(snapshotId);
    if (!snap) return undefined;
    if (ownerId && snap.owner_id !== ownerId) return undefined;
    return snap;
  }

  listProjects(limit: number, offset: number, ownerId: string): ProjectRecord[] {
    const items = Array.from(this.projects.values())
      .filter((p) => p.owner_id === ownerId)
      .sort((a, b) => b.created_at.localeCompare(a.created_at));
    return items.slice(offset, offset + limit);
  }

  saveTask(task: TaskResponse, ownerId: string): TaskResponse {
    const snap = task.snapshot_id ? this.getSnapshot(task.snapshot_id, ownerId) : undefined;
    const projectId = snap?.projectId ?? null;
    const existing = this.tasks.get(task.id);
    const now = new Date().toISOString();
    this.tasks.set(task.id, {
      task,
      owner_id: ownerId,
      project_id: projectId,
      revision: existing ? existing.revision + 1 : 1,
      solution_id: existing?.solution_id ?? null,
      suite_id: existing?.suite_id ?? null,
      validation_id: existing?.validation_id ?? null,
      export_id: existing?.export_id ?? null,
      updated_at: now,
    });
    this.persist();
    return task;
  }

  listTasks(
    filters: {
      limit: number;
      offset: number;
      language?: string;
      category?: string;
      difficulty?: string;
      project_id?: string;
      q?: string;
    },
    ownerId: string
  ) {
    let entries = Array.from(this.tasks.values())
      .filter((e) => e.owner_id === ownerId)
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at));

    if (filters.language) {
      const lang = filters.language.toLowerCase();
      entries = entries.filter((e) => e.task.language.toLowerCase() === lang);
    }
    if (filters.category) {
      const cat = filters.category.toLowerCase();
      entries = entries.filter((e) => e.task.category.toLowerCase() === cat);
    }
    if (filters.difficulty) {
      const diff = filters.difficulty.toLowerCase();
      entries = entries.filter((e) => e.task.difficulty.toLowerCase() === diff);
    }
    if (filters.project_id) {
      entries = entries.filter((e) => e.project_id === filters.project_id);
    }
    if (filters.q) {
      const query = filters.q.toLowerCase();
      entries = entries.filter(
        (e) =>
          e.task.title.toLowerCase().includes(query) ||
          e.task.topic.toLowerCase().includes(query) ||
          (e.task.project_name || '').toLowerCase().includes(query)
      );
    }

    const total = entries.length;
    const page = entries.slice(filters.offset, filters.offset + filters.limit);

    return {
      tasks: page.map((e) => e.task),
      states: page.map((e) => {
        const valArtifact = e.validation_id ? this.artifacts.get(e.validation_id) : undefined;
        const verdict = valArtifact ? (valArtifact.body as ValidationReport).overall_verdict : null;
        return {
          verdict,
          updated_at: e.updated_at,
          project_id: e.project_id,
        };
      }),
      total,
    };
  }

  getTaskDetail(taskId: string, ownerId?: string) {
    const rec = this.tasks.get(taskId);
    if (!rec) return null;
    if (ownerId && rec.owner_id !== ownerId) return null;
    const snap = rec.task.snapshot_id ? this.snapshots.get(rec.task.snapshot_id)?.analysis ?? null : null;
    const solution = rec.solution_id
      ? ((this.artifacts.get(rec.solution_id)?.body as SolutionResponse) ?? null)
      : null;
    const testSuite = rec.suite_id
      ? ((this.artifacts.get(rec.suite_id)?.body as TestSuiteResponse) ?? null)
      : null;
    const validation = rec.validation_id
      ? ((this.artifacts.get(rec.validation_id)?.body as ValidationReport) ?? null)
      : null;
    const savedExport = rec.export_id
      ? ((this.artifacts.get(rec.export_id)?.body as SavedExport) ?? null)
      : null;
    const jobs = Array.from(this.jobs.values())
      .filter((j) => j.task_id === taskId && (!ownerId || j.owner_id === ownerId))
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .slice(0, 10);

    return {
      task: rec.task,
      project_id: rec.project_id,
      revision: rec.revision,
      snapshot: snap,
      solution,
      test_suite: testSuite,
      validation,
      export: savedExport,
      jobs,
    };
  }

  getJob(jobId: string, ownerId?: string): Job | undefined {
    const job = this.jobs.get(jobId);
    if (!job) return undefined;
    if (ownerId && job.owner_id !== ownerId) return undefined;
    return job;
  }

  enqueueJob(
    taskId: string,
    kind: Kind,
    revision: number,
    requestKey: string | undefined,
    ownerId: string
  ): { job: Job; deduplicated: boolean } {
    const rec = this.tasks.get(taskId);
    if (!rec || rec.owner_id !== ownerId) {
      throw Object.assign(new Error('The requested task was not found.'), { statusCode: 404 });
    }
    if (requestKey) {
      const existingId = this.requestKeys.get(`${ownerId}:${taskId}:${requestKey}`);
      if (existingId) {
        const existingJob = this.jobs.get(existingId);
        if (existingJob) return { job: existingJob, deduplicated: true };
      }
    }
    if (rec.revision !== revision) {
      throw Object.assign(
        new Error('Task revision is stale. Reload the latest task state and retry.'),
        { statusCode: 409 }
      );
    }

    const active = Array.from(this.jobs.values()).find(
      (j) => j.task_id === taskId && j.owner_id === ownerId && (j.state === 'queued' || j.state === 'running')
    );
    if (active) {
      throw Object.assign(new Error('Another workflow job is already active for this task.'), {
        statusCode: 409,
      });
    }

    const now = new Date().toISOString();
    const job: Job = {
      id: randomHex(16),
      task_id: taskId,
      owner_id: ownerId,
      kind,
      revision,
      state: 'queued',
      result_id: null,
      cancel_requested: false,
      error: null,
      created_at: now,
      updated_at: now,
    };
    this.jobs.set(job.id, job);
    if (requestKey) {
      this.requestKeys.set(`${ownerId}:${taskId}:${requestKey}`, job.id);
    }
    this.persist();
    return { job, deduplicated: false };
  }

  updateJob(jobId: string, patch: Partial<Job>): Job | undefined {
    const job = this.jobs.get(jobId);
    if (!job) return undefined;
    Object.assign(job, patch, { updated_at: new Date().toISOString() });
    this.persist();
    return job;
  }

  saveArtifact(
    taskId: string,
    kind: Kind,
    id: string,
    body: unknown,
    opts: { status?: string | null; solution_id?: string | null; suite_id?: string | null } = {}
  ): ArtifactRecord {
    const rec = this.tasks.get(taskId);
    const now = new Date().toISOString();
    const artifact: ArtifactRecord = {
      id,
      task_id: taskId,
      owner_id: rec?.owner_id ?? null,
      kind,
      body,
      status: opts.status ?? null,
      solution_id: opts.solution_id ?? rec?.solution_id ?? null,
      suite_id: opts.suite_id ?? rec?.suite_id ?? null,
      created_at: now,
    };
    this.artifacts.set(id, artifact);

    if (rec) {
      rec.revision += 1;
      rec.updated_at = now;
      if (kind === 'solution') {
        rec.solution_id = id;
        rec.validation_id = null;
      } else if (kind === 'tests') {
        rec.suite_id = id;
        rec.validation_id = null;
      } else if (kind === 'validation') {
        rec.validation_id = id;
      } else if (kind === 'export') {
        rec.export_id = id;
      }
    }

    this.persist();
    return artifact;
  }

  getHistory(taskId: string, kind: Kind, limit: number, offset: number, ownerId: string): HistoryItem[] {
    return Array.from(this.artifacts.values())
      .filter((a) => a.task_id === taskId && a.kind === kind && a.owner_id === ownerId)
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .slice(offset, offset + limit)
      .map((a) => ({
        id: a.id,
        created_at: a.created_at,
        status: a.status,
        solution_id: a.solution_id,
        suite_id: a.suite_id,
      }));
  }

  getArtifact(kind: Kind, id: string, ownerId?: string): ArtifactRecord | undefined {
    const artifact = this.artifacts.get(id);
    if (!artifact || artifact.kind !== kind) return undefined;
    if (ownerId && artifact.owner_id !== ownerId) return undefined;
    return artifact;
  }

  saveValidationLog(validationId: string, filename: string, content: string): string {
    const key = `${validationId}/${filename}`;
    this.validationLogs.set(key, content);
    this.persist();
    return key;
  }

  getValidationLog(validationId: string, filename: string, ownerId?: string): string | undefined {
    if (ownerId && !this.getArtifact('validation', validationId, ownerId)) return undefined;
    return this.validationLogs.get(`${validationId}/${filename}`);
  }

  saveEvaluationLog(evaluationId: string, filename: string, content: string): string {
    this.evaluationLogs.set(`${evaluationId}/${filename}`, content);
    this.persist();
    return filename;
  }

  getEvaluationLog(evaluationId: string, filename: string, ownerId?: string): string | undefined {
    if (ownerId && !this.getArtifact('evaluation', evaluationId, ownerId)) return undefined;
    return this.evaluationLogs.get(`${evaluationId}/${filename}`);
  }

  saveExportArchive(id: string, buffer: Buffer, meta: SavedExport) {
    this.exportArchives.set(id, { buffer, meta });
    try {
      fs.mkdirSync(this.exportsDir, { recursive: true });
      fs.writeFileSync(path.join(this.exportsDir, `${id}.zip`), buffer);
    } catch (err) {
      console.warn('[MIRAI Store] Could not write export archive to disk:', err);
    }
    this.persist();
  }

  getExportArchive(id: string, ownerId?: string) {
    if (ownerId && !this.getArtifact('export', id, ownerId)) return undefined;
    return this.exportArchives.get(id);
  }
}

export const store = new DiskBackedWorkflowStore();
export type { EvaluationReport };
