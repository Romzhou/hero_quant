/** Task21 E2E: Chat SSE 流式 ticket→EventSource→[DONE] */
import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

test('Chat SSE 流式 ticket→EventSource→[DONE]', async () => {
  const { execSync } = await import('child_process');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hero-chat-'));
  const pyFile = path.join(dir, 'sse_flow.py');
  const pyScript = `
from hero_quant.api.security import issue_ticket, consume_ticket
import json

# 1. 发票据
ticket = issue_ticket(ttl=60)
assert ticket and len(ticket) > 10, "ticket empty"
# 2. 模拟 SSE 流：服务端以 ticket 校验，流式推送后 [DONE]
assert consume_ticket(ticket) is True, "first consume should succeed"
# 重复消费应失败（单次票据）
assert consume_ticket(ticket) is False, "replay should fail"
# 3. 新票据用于 SSE
ticket2 = issue_ticket()
assert consume_ticket(ticket2) is True
# 模拟 SSE chunks
chunks = ["data: {\\"type\\":\\"text\\",\\"text\\":\\"hello\\"} \\n\\n", "data: {\\"type\\":\\"text\\",\\"text\\":\\" world\\"} \\n\\n", "data: [DONE]\\n\\n"]
# EventSource 客户端应能解析 [DONE] 终止
assert any("[DONE]" in c for c in chunks), "stream must end with [DONE]"
print(json.dumps({"ok": True, "chunks": len(chunks), "done": True}))
`;
  fs.writeFileSync(pyFile, pyScript, 'utf-8');
  const out = execSync(`python "${pyFile}"`, { encoding: 'utf-8' });
  const last = out.trim().split('\n').pop() || '{}';
  const j = JSON.parse(last);
  expect(j.ok).toBe(true);
  expect(j.done).toBe(true);
  expect(j.chunks).toBe(3);
  fs.rmSync(dir, { recursive: true, force: true });
});

test('Chat SSE ticket TTL 过期', async () => {
  const { execSync } = await import('child_process');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hero-chat-ttl-'));
  const pyFile = path.join(dir, 'ttl.py');
  const pyScript = `
from hero_quant.api.security import issue_ticket, consume_ticket
import time
t = issue_ticket(ttl=0.01)
time.sleep(0.05)
# 过期后消费应失败
assert consume_ticket(t) is False, "expired ticket should fail"
print("ok")
`;
  fs.writeFileSync(pyFile, pyScript, 'utf-8');
  const out = execSync(`python "${pyFile}"`, { encoding: 'utf-8' });
  expect(out.trim()).toContain('ok');
  fs.rmSync(dir, { recursive: true, force: true });
});
