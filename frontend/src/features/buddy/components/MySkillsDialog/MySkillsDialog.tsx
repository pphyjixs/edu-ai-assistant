import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { userSkillsApi } from "@/features/buddy/api/skills";
import { HttpError, toAppError } from "@/services/http";

import styles from "./MySkillsDialog.module.css";

export function MySkillsDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["user-skills"], queryFn: userSkillsApi.list });
  const fileInput = useRef<HTMLInputElement>(null);
  const [category, setCategory] = useState("material-summary");
  const [error, setError] = useState("");
  const upload = useMutation({
    mutationFn: async (file: File) => {
      if (!file.name.toLowerCase().endsWith(".md")) throw new Error("请选择 Markdown 文件（.md）");
      if (file.size > 40 * 1024) throw new Error("Skill 文件不能超过 40 KiB");
      return userSkillsApi.upload(await file.text(), category);
    },
    onSuccess: () => {
      setError("");
      void queryClient.invalidateQueries({ queryKey: ["user-skills"] });
      if (fileInput.current) fileInput.current.value = "";
    },
    onError: (cause) => setError(cause instanceof Error && !(cause instanceof HttpError) ? cause.message : toAppError(cause).message),
  });
  const remove = useMutation({
    mutationFn: userSkillsApi.remove,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["user-skills"] }),
    onError: (cause) => setError(toAppError(cause).message),
  });

  return (
    <div className={styles.backdrop} onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className={styles.dialog} role="dialog" aria-modal="true" aria-label="我的 Skill">
        <header className={styles.header}>
          <div><h2>我的 Skill</h2><p>上传 Markdown 工作流，按功能在对话中自动选用，也可手动指定。</p></div>
          <button type="button" onClick={onClose} aria-label="关闭">×</button>
        </header>
        <div className={styles.upload}>
          <label>功能分类
            <select value={category} onChange={(event) => setCategory(event.target.value)}>
              {Object.entries(query.data?.categories ?? {
                "material-summary": "总结课件", "practice-generation": "生成题目",
                "lesson-preview": "课前预习", "review-plan": "复习巩固",
                "assignment-guidance": "作业指导", other: "其他",
              }).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
          </label>
          <label>上传 Skill.md
            <input ref={fileInput} type="file" accept=".md,text/markdown,text/plain"
              onChange={(event) => { const file = event.target.files?.[0]; if (file) upload.mutate(file); }} />
          </label>
        </div>
        <p className={styles.hint}>文件需以 --- frontmatter 开头，包含 name、description、version 和文字正文；正文最多 32 KiB。</p>
        {upload.isPending && <p>正在上传…</p>}
        {error && <p className={styles.error} role="alert">{error}</p>}
        <div className={styles.list}>
          <h3>已上传的 Skill</h3>
          {query.isPending && <p>加载中…</p>}
          {query.isError && <p className={styles.error}>加载失败，请重试。</p>}
          {query.data?.items.length === 0 && <p>还没有上传 Skill。</p>}
          {query.data?.items.map((skill) => (
            <article key={skill.id} className={styles.item}>
              <div><strong>{skill.name}</strong><small>{query.data.categories[skill.category] ?? skill.category} · v{skill.version}</small><p>{skill.description}</p></div>
              <button type="button" disabled={remove.isPending} onClick={() => remove.mutate(skill.id)}>删除</button>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}
