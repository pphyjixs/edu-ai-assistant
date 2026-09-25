import { http } from "@/services/http";

export type UserSkill = {
  id: string;
  name: string;
  description: string;
  category: string;
  version: string;
  created_at: string;
};

export type SkillList = {
  items: UserSkill[];
  system_items: { name: string; description: string }[];
  categories: Record<string, string>;
};

export const userSkillsApi = {
  list: () => http.get<SkillList>("/users/me/skills"),
  upload: (content: string, category: string) =>
    http.post<UserSkill>("/users/me/skills", { content, category }),
  remove: (id: string) => http.delete<void>(`/users/me/skills/${id}`),
};
