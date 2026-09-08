const BASE = '/api';

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, { cache: 'no-store', ...init });
  if (!r.ok) {
    // 后端 HTTPException 带 detail，透出比裸状态码有用得多
    let msg = `${r.status} ${path}`;
    try {
      const body = await r.json();
      if (body?.detail) msg = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch { /* 非 JSON 响应体，维持默认消息 */ }
    throw new ApiError(r.status, msg);
  }
  return r.json() as Promise<T>;
}

export function get<T>(path: string): Promise<T> {
  return request<T>(path);
}

export function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function del<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'DELETE' });
}

/** SWR 共用 fetcher */
export const fetcher = <T,>(path: string) => get<T>(path);
