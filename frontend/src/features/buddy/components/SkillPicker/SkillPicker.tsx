import { useQuery } from "@tanstack/react-query";

import { userSkillsApi } from "@/features/buddy/api/skills";

import styles from "./SkillPicker.module.css";

export function SkillPicker({
  value,
  onChange,
  disabled = false,
}: {
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
}) {
  const skills = useQuery({ queryKey: ["user-skills"], queryFn: userSkillsApi.list });
  return (
    <label className={styles.label}>
      <span>本次 Skill</span>
      <select
        aria-label="本次对话使用的 Skill"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled || skills.isPending}
      >
        <option value="">自动选择</option>
        {skills.data?.system_items.map((skill) => (
          <option key={skill.name} value={`system:${skill.name}`}>
            {skill.name} · 内置 Skill
          </option>
        ))}
        {skills.data?.items.map((skill) => (
          <option key={skill.id} value={`user:${skill.id}`}>
            {skill.name} · {skills.data.categories[skill.category] ?? skill.category}
          </option>
        ))}
      </select>
    </label>
  );
}
