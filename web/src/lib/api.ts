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

/** 封套接口（{code,data,message,trace_id}）取数：成功解出 data，失败抛 ApiError。
 * 旧接口（裸返回）原样透传 —— 新老共存，一处函数两种契约都安全。 */
async function requestData<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, { cache: 'no-store', ...init });
  if (!r.ok) {
    let msg = `${r.status} ${path}`;
    try {
      const body = await r.json();
      if (body?.message) msg = body.message;
      else if (body?.detail)
        msg = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch { /* 非 JSON 错误体，维持默认消息 */ }
    throw new ApiError(r.status, msg);
  }
  const body: unknown = await r.json();
  if (body && typeof body === 'object' && 'code' in (body as Record<string, unknown>)) {
    const env = body as { code: number; data?: T; message?: string };
    if (env.code !== 0) throw new ApiError(200, env.message || '操作失败');
    return env.data as T;
  }
  return body as T;
}

export function getData<T>(path: string): Promise<T> {
  return requestData<T>(path);
}

export function putData<T>(path: string, body: unknown): Promise<T> {
  return requestData<T>(path, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function delData<T>(path: string): Promise<T> {
  return requestData<T>(path, { method: 'DELETE' });
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

/** SWR 封套 fetcher（解包 code/data） */
export const fetcherData = <T,>(path: string) => getData<T>(path);
