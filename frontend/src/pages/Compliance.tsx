/**
 * Compliance 合规与免责页（PR-172）
 *
 * - 只读静态页：内容镜像 `docs/COMPLIANCE.md` 0.2.0 的第 1–4 节 + 版本区，措辞不新增承诺、不夸大。
 * - 入口：App 页脚「合规与免责」+ 设置页令牌卡片旁的链接。本页**不进 RouteGuard**：
 *   合规文本需要在任何角色、甚至未配置令牌时都可读（否则低权用户看不到自己受什么约束）。
 * - 版本号与日期是静态字符串，禁止 `new Date()`：否则测试会随机器时间漂移；
 *   文档升版时须同步改 `COMPLIANCE_DOC_VERSION` / `COMPLIANCE_DOC_ALIGNED_AT`。
 * - 注意：`COMPLIANCE_DOC_VERSION` 是**文档版本**，与 `config/version.ts` 的 `APP_VERSION`
 *   （package.json 里的产品版本）是两个口径，不参与 version.test.ts 的单源锁定。
 */
import { Link } from "react-router-dom"

/** 合规文本版本：与 docs/COMPLIANCE.md 文件头 `> 版本 0.2.0` 对齐 */
export const COMPLIANCE_DOC_VERSION = "0.2.0"
/** 本页文案与文档对齐的日期（静态字符串，便于审计追溯） */
export const COMPLIANCE_DOC_ALIGNED_AT = "2026-10-31"
/** 权威文本位置：页面与文档冲突时以此为准 */
export const COMPLIANCE_DOC_REF = "docs/COMPLIANCE.md"

export type CompliancePoint = { lead: string; text: string }
export type ComplianceSection = { id: string; title: string; points: CompliancePoint[] }

/**
 * 五个分区：投资免责 / 数据来源与限制 / 合成模式 / 隐私与令牌 / 版本与同步。
 * 每条都对应 docs/COMPLIANCE.md 的既有表述，未新增任何能力或承诺。
 */
export const COMPLIANCE_SECTIONS: ComplianceSection[] = [
  {
    id: "investment-disclaimer",
    title: "投资免责",
    points: [
      {
        lead: "非投资建议",
        text: "本系统是研究与教育工具：行情解读、指标计算、回测结果、报告与自然语言回答均不构成任何证券、基金、期货、外汇或加密资产的投资建议、要约或承诺。",
      },
      {
        lead: "模型可能幻觉",
        text: "LLM 可能产生幻觉或引用过时信息；系统已引入 Grounding 证据账本与 provenance 标注降低风险，但关键结论仍需人工复核。",
      },
      {
        lead: "交易决策自负",
        text: "任何交易决策由用户自主做出，开发与运营方不对盈亏承担责任；实盘前请自行尽调并做小规模验证。",
      },
    ],
  },
  {
    id: "data-sources",
    title: "数据来源与限制",
    points: [
      {
        lead: "主要行情源",
        text: "A 股以腾讯（qt.gtimg.cn）为主、美股以 Yahoo（yfinance）为主；注册表登记 16 个数据源。可选 tushare / akshare / ccxt 需用户自备合规许可与配额。",
      },
      {
        lead: "顺序回退不等于择优",
        text: "取数按注册顺序回退，没有打分、没有「自动切到最优源」；数据层也没有熔断器（CircuitBreaker 只用于 telemetry 与 shadow / mcp 消费方），请不要按「熔断后择优切换」理解。",
      },
      {
        lead: "跨源校验的触发条件",
        text: "收盘价 1% 阈值校验仅在**同一标的拿到两个可比源**时才执行；A 股与美股的市场与单位（手 board_lots / 股 shares）本就互斥，因此生产路径通常不触发。",
      },
      {
        lead: "使用约束",
        text: "需遵守各数据提供方的服务条款、速率限制与再分发限制；对外发布基于本系统的结论时，应注明数据来源、时间区间与 HERO_DATA_MODE。",
      },
    ],
  },
  {
    id: "synthetic-mode",
    title: "合成模式",
    points: [
      {
        lead: "显式打标",
        text: "HERO_DATA_MODE=synthetic 可用，但产物会标记 provenance.source=synthetic，并在界面标注为合成；合成序列不代表真实行情。",
      },
      {
        lead: "校验显式跳过",
        text: "合成模式下跨源 1% 比对被显式跳过并记 warning（cross_source_synthetic_skipped），不是「比对通过」。",
      },
      {
        lead: "不静默回退",
        text: "合成与 live 数据混用需显式 opt-in，否则 fail-closed 拒绝；live 取数失败不会静默回退为合成结果。",
      },
    ],
  },
  {
    id: "privacy-token",
    title: "隐私与令牌",
    points: [
      {
        lead: "只存本机",
        text: "身份令牌只存本机浏览器 localStorage（键 hero-quant.token），不上传第三方、不写入日志；清除令牌即回到匿名链路。",
      },
      {
        lead: "后端才是裁决方",
        text: "前端只在请求头附加 Authorization: Bearer；令牌是否有效、租户与角色是否被接受，一律由后端裁决，前端无权判定。",
      },
      {
        lead: "解析结果不验签",
        text: "设置页展示的租户 / 用户 / 到期 / 角色来自令牌声明的明文解析，未验签，仅供展示，不得当作安全边界。",
      },
    ],
  },
  {
    id: "version",
    title: "版本与同步",
    points: [
      {
        lead: "本次对齐",
        text: `本页文案与 ${COMPLIANCE_DOC_REF} 的对齐日期：${COMPLIANCE_DOC_ALIGNED_AT}`,
      },
      {
        lead: "冲突以文档为准",
        text: `本页是 ${COMPLIANCE_DOC_REF} 第 1–4 节的只读摘要；如与文档不一致，以仓库内文档为准，重大变更会在 CHANGELOG 与 README 公告。`,
      },
    ],
  },
]

/** 单条要点：琥珀色 lead + 正文，保持与设置页卡片一致的深墨/低噪声排版 */
function PointRow({ point }: { point: CompliancePoint }) {
  return (
    <li className="flex gap-2 text-sm leading-6 text-slate-300">
      <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-amber-400/70" aria-hidden="true" />
      <span>
        <span className="font-semibold text-mist">{point.lead}：</span>
        {point.text}
      </span>
    </li>
  )
}

export default function Compliance() {
  return (
    <div className="mx-auto max-w-3xl px-6 py-6" data-testid="compliance-page">
      <h1 className="font-display text-xl font-semibold text-mist">合规与免责</h1>
      {/* 版本行用单模板字符串渲染：使整句成为单个文本节点，测试可按整句断言，版本号也只有一个来源 */}
      <p data-testid="compliance-version" className="mt-1 text-sm text-slate-400">
        {`合规文本版本 ${COMPLIANCE_DOC_VERSION} · 与 ${COMPLIANCE_DOC_REF} 同步`}
      </p>
      <p className="mt-1 text-xs text-slate-500">
        只读说明 · 任何角色均可访问（无需令牌）· 本文本不构成法律意见，部署与使用方需自行评估所在地监管要求
      </p>

      <div className="mt-5 rounded-2xl border border-amber-500/20 bg-amber-500/[0.07] px-4 py-3 text-xs leading-5 text-amber-200">
        本系统是投研辅助工具，输出结果不构成投资建议；数据可能来自合成模式，且模型可能幻觉，关键结论请人工复核。
      </div>

      <div className="mt-5 space-y-5">
        {COMPLIANCE_SECTIONS.map(section => (
          <section
            key={section.id}
            data-testid={`compliance-section-${section.id}`}
            className="rounded-2xl border border-white/10 bg-ink-800/60 p-5 backdrop-blur"
          >
            <h2 className="text-sm font-semibold text-mist">{section.title}</h2>
            <ul className="mt-3 space-y-2.5">
              {section.points.map(point => (
                <PointRow key={point.lead} point={point} />
              ))}
            </ul>
          </section>
        ))}
      </div>

      <div className="mt-5 rounded-2xl border border-white/10 bg-white/[0.04] px-4 py-3 text-xs leading-5 text-slate-500">
        本页只读，不接受任何配置写入；如需更换令牌或调整本地偏好，请前往
        <Link to="/settings" className="mx-1 text-slate-300 underline decoration-dotted hover:text-mist">
          设置页
        </Link>
        。权威文本为仓库内 <span className="font-mono text-slate-300">{COMPLIANCE_DOC_REF}</span>。
      </div>
    </div>
  )
}
