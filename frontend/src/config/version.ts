/**
 * 前端版本单源（E线）
 * - 唯一来源：`frontend/package.json` 的 `version` 字段；组件侧禁止写字面版本号。
 * - 跨语言一致性（前端 package.json ↔ 后端 pyproject.toml）由
 *   `src/__tests__/version.test.ts` 锁定，避免再次出现 0.2.0 / 0.3.0 漂移。
 */
import pkg from "../../package.json"

export const APP_VERSION: string = pkg.version
