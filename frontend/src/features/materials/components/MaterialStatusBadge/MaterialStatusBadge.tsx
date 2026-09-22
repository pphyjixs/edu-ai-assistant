/** 资料解析状态标签。PROCESSING 展示为「解析中」而不是失败（契约 4.7）。 */

import { Pill } from "@/components/Pill/Pill";
import type { MaterialVM } from "@/features/materials/model/types";

export function MaterialStatusBadge({ material }: { material: MaterialVM }) {
  return <Pill tone={material.statusTone}>{material.statusLabel}</Pill>;
}
