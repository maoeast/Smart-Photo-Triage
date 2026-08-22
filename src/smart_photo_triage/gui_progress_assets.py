"""Small, independently loaded progress UI for long-running local copies."""

COPY_PROGRESS_JS = r"""
(() => {
  const title = document.getElementById("copy-progress-title");
  const detail = document.getElementById("copy-progress-detail");
  if (!title || !detail) return;
  const formatBytes = value => {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0, amount = Number(value || 0);
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
    return `${amount.toFixed(index ? 1 : 0)} ${units[index]}`;
  };
  const render = job => {
    if (!job) return;
    title.textContent = job.state === "RUNNING" ? "正在安全复制" : `复制状态：${job.state}`;
    const eta = job.remaining_seconds == null
      ? "正在估算剩余时间" : `约剩 ${job.remaining_seconds} 秒`;
    const counts = `${job.completed_files}/${job.total_files} 个文件`;
    const bytes = `${formatBytes(job.copied_bytes)}/${formatBytes(job.total_bytes)}`;
    const recovery = job.recoverable ? "，可用恢复功能继续" : "";
    const speed = `${formatBytes(job.bytes_per_second)}/秒`;
    const status = `${counts} · ${bytes} · ${speed} · ${eta} · 失败 ${job.failed_files}${recovery}`;
    detail.textContent = `${job.current_file || "正在整理事务记录"}\n${status}`;
  };
  const poll = async () => {
    try {
      const response = await fetch("/api/copy-job", {cache: "no-store"});
      if (response.ok) render((await response.json()).job);
    } catch (_) { /* a closing local server does not change file state */ }
  };
  poll();
  window.setInterval(poll, 750);
})();
"""
