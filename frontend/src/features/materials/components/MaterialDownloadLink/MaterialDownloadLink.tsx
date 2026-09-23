/**
 * 资料原文的下载入口（契约 4.9）。
 *
 * 下载地址是**短时有效**的预签名 GET（默认 10 分钟），因此这里不缓存链接：
 * 每次进入页面取一次，并显式展示失效时间；用户看到"地址已过期"时可以一键重取，
 * 而不是对着一个打不开的链接反复点。
 */

import { Button } from "@/components/Button/Button";
import { useMaterialDownloadUrl } from "@/features/materials/hooks/useMaterials";
import { toAppError } from "@/services/http";

import styles from "./MaterialDownloadLink.module.css";

export type MaterialDownloadLinkProps = {
  materialId: string;
  filename: string;
};

export function MaterialDownloadLink({ materialId, filename }: MaterialDownloadLinkProps) {
  const download = useMaterialDownloadUrl(materialId);

  if (download.isPending) {
    return <span className={styles.hint}>正在准备下载地址…</span>;
  }

  if (download.isError) {
    const error = toAppError(download.error);

    return (
      <span className={styles.failed}>
        <span className={styles.hint}>
          {error.code === "SERVICE_UNAVAILABLE"
            ? "对象存储暂时不可用，无法生成下载地址。"
            : "下载地址获取失败。"}
        </span>
        <Button variant="ghost" size="sm" onClick={() => void download.refetch()}>
          重试
        </Button>
      </span>
    );
  }

  if (!download.data) return null;

  return (
    <span className={styles.wrap}>
      <a
        className={styles.link}
        href={download.data.url}
        // 原文件由对象存储直接返回，浏览器会按 MIME 决定预览还是下载
        target="_blank"
        rel="noreferrer"
        download={filename}
      >
        下载原文件
      </a>
      <span className={styles.hint}>地址有效期至 {download.data.expiresAtLabel}</span>
      <Button variant="ghost" size="sm" onClick={() => void download.refetch()}>
        刷新地址
      </Button>
    </span>
  );
}
