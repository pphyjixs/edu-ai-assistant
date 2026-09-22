/**
 * 记住自己提交过的答题记录 ID。
 *
 * 契约里没有「按练习查我的答题记录」的接口——`GET /practice-attempts/{id}`
 * 需要先有 attempt_id，而它只在提交成功（7.6）的响应里出现过。
 * 因此这里把「练习 → 我的答题记录」记在本地，让用户刷新页面后仍能回到结果页。
 *
 * 这只是本地索引，不缓存任何业务数据；记录失效（404）时会被清掉。
 */

const STORAGE_KEY = "studybuddy.practice-attempts";

type AttemptIndex = Record<string, string>;

function read(): AttemptIndex {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" ? (parsed as AttemptIndex) : {};
  } catch {
    return {};
  }
}

function write(index: AttemptIndex): void {
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(index));
}

export const attemptIndex = {
  get(practiceSetId: string): string | undefined {
    return read()[practiceSetId];
  },
  save(practiceSetId: string, attemptId: string): void {
    write({ ...read(), [practiceSetId]: attemptId });
  },
  forget(practiceSetId: string): void {
    const index = read();
    if (!(practiceSetId in index)) return;
    delete index[practiceSetId];
    write(index);
  },
};
