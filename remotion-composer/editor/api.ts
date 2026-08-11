// 编辑器 API 客户端——从 App.tsx / AuthoredEditor.tsx 各自维护的一份逐字复制
// 里提出来（两边完全一致，纯粹是各写各的历史遗留）。所有编辑器请求都走
// 这两个函数：query string 带 token（跟服务端 _require_editor_token 的
// 约定一致），JSON in / JSON out，404 之外的失败统一 throw 一个带
// `.status` 的 Error，方便调用方按状态码分流（429 限流 / 409 冲突等）。

export async function apiGet(path: string, token: string): Promise<any> {
  const resp = await fetch(`${path}?token=${encodeURIComponent(token)}`);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data?.detail || `HTTP ${resp.status}`);
  return data;
}

export async function apiPost(path: string, token: string, body: unknown): Promise<any> {
  const resp = await fetch(`${path}?token=${encodeURIComponent(token)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const err = new Error(data?.detail || `HTTP ${resp.status}`) as Error & { status?: number };
    err.status = resp.status;
    throw err;
  }
  return data;
}
