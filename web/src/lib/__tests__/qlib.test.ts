import { describe, expect, it, vi, beforeEach } from 'vitest';
import { getQlibStatus, compareQlibRuns, EXPORT_FIELDS } from '../qlib';
import * as api from '../api';

vi.mock('../api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  postData: vi.fn(),
  putData: vi.fn(),
}));

import { get, post, postData, putData } from '../api';

describe('qlib api', () => {
  beforeEach(() => vi.clearAllMocks());

  it('getQlibStatus hits /qlib/status', async () => {
    vi.mocked(get).mockResolvedValueOnce({ exists: false });
    await getQlibStatus();
    expect(get).toHaveBeenCalledWith('/qlib/status');
  });

  it('compareQlibRuns posts ids', async () => {
    vi.mocked(postData).mockResolvedValueOnce({ runs: [], rows: [] });
    await compareQlibRuns(['a', 'b']);
    expect(postData).toHaveBeenCalledWith('/qlib/runs/compare', { ids: ['a', 'b'] });
  });

  it('EXPORT_FIELDS covers default set', () => {
    expect(EXPORT_FIELDS).toContain('close');
    expect(EXPORT_FIELDS).toContain('vwap');
    expect(EXPORT_FIELDS).toContain('pe_ttm');
  });
});
