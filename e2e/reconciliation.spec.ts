/**
 * 对账 E2E（真浏览器 + 真 python 服务）
 * - JS 镜像 / python 服务双向校验：shadow ledger 与 positions.csv 0 差额 / 差额可检出
 * - 浏览器段：真 goto /risk，断言 ShadowAccount 对账面板渲染（前端不再只靠读源码字符串）
 */
import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import { execFileSync } from 'child_process';

// Pure JS mirror of src/hero_quant/governance/reconcile.py
function loadPositionsCsv(csvPath: string): Record<string, number> {
  const text = fs.readFileSync(csvPath, 'utf-8');
  const lines = text.split(/\r?\n/).filter(l => l.trim().length > 0);
  if (lines.length === 0) throw new Error('empty csv');
  const header = lines[0].split(',').map(h => h.trim().toLowerCase());
  const symIdx = header.findIndex(h => ['symbol', 'instrument', 'code', 'ticker', 'asset'].includes(h));
  const qtyIdx = header.findIndex(h => ['qty', 'quantity', 'position', 'amount', 'shares', 'holding', 'vol'].includes(h));
  const sIdx = symIdx >= 0 ? symIdx : 0;
  const qIdx = qtyIdx >= 0 ? qtyIdx : header.length > 1 ? 1 : 0;
  const out: Record<string, number> = {};
  for (let i = 1; i < lines.length; i++) {
    const cols = lines[i].split(',').map(c => c.trim());
    const sym = cols[sIdx];
    if (!sym) continue;
    const qty = parseFloat(cols[qIdx] || '0');
    out[sym] = (out[sym] || 0) + (isNaN(qty) ? 0 : qty);
  }
  return out;
}

function aggregateShadowFromLedger(ledgerPath: string): Record<string, number> {
  const out: Record<string, number> = {};
  if (!fs.existsSync(ledgerPath)) return out;
  const text = fs.readFileSync(ledgerPath, 'utf-8');
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const e = JSON.parse(trimmed);
      const rec = e.record || {};
      if (rec.action === 'shadow_record' && rec.trade) {
        const sym = String(rec.trade.symbol || '').trim();
        const qty = parseFloat(String(rec.trade.qty ?? rec.trade.quantity ?? 0));
        if (sym) out[sym] = (out[sym] || 0) + (isNaN(qty) ? 0 : qty);
      } else if (rec.symbol && (rec.qty !== undefined || rec.quantity !== undefined)) {
        const sym = String(rec.symbol).trim();
        const qty = parseFloat(String(rec.qty ?? rec.quantity ?? 0));
        if (sym) out[sym] = (out[sym] || 0) + (isNaN(qty) ? 0 : qty);
      }
    } catch {
      /* ignore corrupt line */
    }
  }
  return out;
}

function reconcile(shadow: Record<string, number>, broker: Record<string, number>, tolerance = 1e-6) {
  const all = new Set([...Object.keys(shadow), ...Object.keys(broker)]);
  const diffs: Array<{ symbol: string; shadow: number; broker: number; diff: number }> = [];
  let total = 0;
  for (const sym of Array.from(all).sort()) {
    const s = shadow[sym] ?? 0;
    const b = broker[sym] ?? 0;
    const d = s - b;
    total += Math.abs(d);
    if (Math.abs(d) > tolerance) diffs.push({ symbol: sym, shadow: s, broker: b, diff: d });
  }
  return { shadow, positions: broker, diffs, zero_diff: diffs.length === 0, total_diff: Math.round(total * 1e10) / 1e10 };
}

function mkTempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hero-reconcile-'));
}

/** 仓库自带 venv 优先；宿主无 python 别名（只有 python3） */
function pythonBin(): string {
  const venv = path.join(process.cwd(), '.venv', 'bin', 'python');
  return fs.existsSync(venv) ? venv : 'python3';
}

test('daily shadow ledger vs positions 0差额', async () => {
  const dir = mkTempDir();
  const ledgerPath = path.join(dir, 'shadow_ledger.jsonl');
  const positionsPath = path.join(dir, 'positions.csv');

  const trades = [
    { symbol: '600519.SH', qty: 100, price: 10, side: 'buy' },
    { symbol: '000001.SZ', qty: 200, price: 8, side: 'buy' },
  ];
  const ledgerLines = trades
    .map((t, i) =>
      JSON.stringify({
        seq: i + 1,
        tenant_seq: i + 1,
        tenant: 'default',
        prev_hash: '0'.repeat(64),
        record_hash: 'hash' + i,
        record: { action: 'shadow_record', trade: t },
      })
    )
    .join('\n');
  fs.writeFileSync(ledgerPath, ledgerLines, 'utf-8');
  fs.writeFileSync(positionsPath, 'symbol,qty\n600519.SH,100\n000001.SZ,200\n', 'utf-8');

  const result = reconcile(aggregateShadowFromLedger(ledgerPath), loadPositionsCsv(positionsPath));
  expect(result.zero_diff).toBe(true);
  expect(result.total_diff).toBe(0);
  expect(result.diffs).toHaveLength(0);
  expect(Object.keys(result.shadow)).toHaveLength(2);

  fs.rmSync(dir, { recursive: true, force: true });
});

test('reconciliation detects mismatch', async () => {
  const dir = mkTempDir();
  const ledgerPath = path.join(dir, 'shadow_ledger.jsonl');
  const positionsPath = path.join(dir, 'positions.csv');

  fs.writeFileSync(
    ledgerPath,
    JSON.stringify({
      seq: 1,
      tenant_seq: 1,
      tenant: 'default',
      prev_hash: '0'.repeat(64),
      record_hash: 'h1',
      record: { action: 'shadow_record', trade: { symbol: '600519.SH', qty: 100 } },
    }) + '\n',
    'utf-8'
  );
  fs.writeFileSync(positionsPath, 'symbol,quantity\n600519.SH,90\n', 'utf-8');

  const result = reconcile(aggregateShadowFromLedger(ledgerPath), loadPositionsCsv(positionsPath));
  expect(result.zero_diff).toBe(false);
  expect(result.diffs).toHaveLength(1);
  expect(result.diffs[0].symbol).toBe('600519.SH');
  expect(result.diffs[0].diff).toBe(10);
  expect(result.total_diff).toBe(10);

  fs.rmSync(dir, { recursive: true, force: true });
});

test('python governance/reconcile 与 JS 镜像一致（0差额）', async () => {
  const dir = mkTempDir();
  const ledgerPath = path.join(dir, 'ledger.jsonl');
  const positionsPath = path.join(dir, 'positions.csv');
  const pyFile = path.join(dir, 'run_reconcile.py');

  const pyScript = `
from pathlib import Path
from hero_quant.governance.ledger import Ledger
from hero_quant.shadow import ShadowJournal
from hero_quant.governance.reconcile import daily_reconciliation
import json, csv
ledger_path = Path(r"${ledgerPath}")
positions_csv = Path(r"${positionsPath}")
ledger = Ledger(ledger_path)
j = ShadowJournal(ledger=ledger)
j.record({"symbol":"600519.SH","qty":150,"price":12,"side":"buy"})
j.record({"symbol":"BTC/USDT","qty":2,"price":30000,"side":"buy"})
with positions_csv.open("w", newline="", encoding="utf-8") as f:
    w=csv.writer(f)
    w.writerow(["symbol","qty"])
    w.writerow(["600519.SH",150])
    w.writerow(["BTC/USDT",2])
report = daily_reconciliation(date="2026-08-21", ledger_path=ledger_path, positions_csv=positions_csv)
print(json.dumps(report))
`;
  fs.writeFileSync(pyFile, pyScript, 'utf-8');

  const out = execFileSync(pythonBin(), [pyFile], { encoding: 'utf-8', cwd: path.join(process.cwd()) });
  const report = JSON.parse(out.trim().split('\n').pop() || '{}');
  expect(report.zero_diff).toBe(true);
  expect(report.total_diff).toBe(0);
  expect(report.date).toBe('2026-08-21');
  expect(report.diffs).toHaveLength(0);

  const shadowJs = aggregateShadowFromLedger(ledgerPath);
  expect(shadowJs['600519.SH']).toBe(150);
  expect(shadowJs['BTC/USDT']).toBe(2);

  fs.rmSync(dir, { recursive: true, force: true });
});

test('风控页渲染 ShadowAccount 对账面板（真浏览器）', async ({ page }) => {
  await page.route('**/v1/**', route => route.abort());
  await page.goto('/risk');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('Risk · 风控');
  await expect(page.getByText(/ShadowAccount/).first()).toBeVisible();
});
