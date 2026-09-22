/**
 * 记住自己生成的练习（教师视角）。
 *
 * 契约 7.3 的列表**只返回 `PUBLISHED`**，而教师生成出来的练习先是
 * `DRAFT`——也就是说，生成之后没有任何接口能把它列出来。
 * 唯一能拿到新练习 ID 的地方是生成响应里任务的 `resource_id`，
 * 因此这里把这个 ID 记在本地，让教师能回到草稿页去发布或重试。
 *
 * 只存 ID 与生成时间，不缓存题目内容；详情仍从接口读取。
 */

const STORAGE_KEY = "studybuddy.generated-practice-sets";

export type GeneratedPracticeSet = {
  setId: string;
  jobId: string;
  createdAt: string;
};

type GeneratedIndex = Record<string, GeneratedPracticeSet[]>;

function read(): GeneratedIndex {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" ? (parsed as GeneratedIndex) : {};
  } catch {
    return {};
  }
}

function write(index: GeneratedIndex): void {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(index));
}

export const generatedPracticeIndex = {
  list(courseId: string): GeneratedPracticeSet[] {
    return read()[courseId] ?? [];
  },
  add(courseId: string, entry: GeneratedPracticeSet): void {
    const index = read();
    const current = index[courseId] ?? [];
    if (current.some((item) => item.setId === entry.setId)) return;
    write({ ...index, [courseId]: [entry, ...current].slice(0, 20) });
  },
  remove(courseId: string, setId: string): void {
    const index = read();
    const current = index[courseId] ?? [];
    write({ ...index, [courseId]: current.filter((item) => item.setId !== setId) });
  },
};
